"""PR31 — restricted generic internal email orchestration for POST /sendemail.

JWT identifies the invoking account; ``X-OpenBid-Internal-Key`` proves internal
authorization. Recipient/sender policies are server-configured. Plain-text only.
No CC/BCC/attachments. Fail-closed rate limits. Redacted structured audit logs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..models.user_table import User
from ..utils.common import InternalEmailPurpose, InternalEmailSendRequest
from ..utils.email import send_email
from ..utils.rate_limit import enforce_rate_limit

_logger = logging.getLogger(__name__)

_DEFAULT_FROM = "customersupport@wizzride.com"
_SUPPORTED_FROM = frozenset(
    {
        "reservations@wizzride.com",
        "customersupport@wizzride.com",
    }
)

_SUBJECT_MAX = 200
_MESSAGE_MAX = 20_000

_CALLER_PER_MIN = 5
_CALLER_PER_HOUR = 30
_CALLER_PER_DAY = 100
_RECIPIENT_PER_HOUR = 10
_DOMAIN_PER_HOUR = 20
_DUP_WINDOW_SECONDS = 300

# Disallowed controls: C0 + DEL, but allow TAB (0x09) and LF (0x0a) in message body.
_SUBJECT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MESSAGE_DISALLOWED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InternalEmailError(Exception):
    """Typed internal-email failure with HTTP status + stable detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _hash_part(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def _is_tombstone_user_app_id(user_app_id: Optional[str]) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()


def _latency_bucket(elapsed_ms: float) -> str:
    if elapsed_ms < 100:
        return "0-100ms"
    if elapsed_ms < 500:
        return "100-500ms"
    if elapsed_ms < 2000:
        return "500-2000ms"
    if elapsed_ms < 5000:
        return "2-5s"
    return "5s+"


def _audit(
    *,
    outcome: str,
    caller_id: Optional[str],
    purpose: Optional[str],
    recipient_domain: Optional[str],
    recipient_hash: Optional[str],
    subject_len: Optional[int],
    body_len: Optional[int],
    latency_ms: Optional[float],
    used_fallback: Optional[bool] = None,
) -> None:
    payload = {
        "event": "internal_email_send",
        "outcome": outcome,
        "caller_hash": _hash_part(caller_id) if caller_id else None,
        "purpose": purpose,
        "recipient_domain": recipient_domain,
        "recipient_hash": recipient_hash,
        "subject_len": subject_len,
        "body_len": body_len,
        "latency_bucket": _latency_bucket(latency_ms) if latency_ms is not None else None,
        "used_fallback": used_fallback,
    }
    _logger.info("internal_email_audit %s", payload)


def _normalize_domain(domain: str, *, as_config: bool = False) -> str:
    raw = (domain or "").strip().lower().rstrip(".")
    err = (
        InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID")
        if as_config
        else InternalEmailError(403, "INTERNAL_EMAIL_RECIPIENT_NOT_ALLOWED")
    )
    if not raw or " " in raw or "\n" in raw or "\r" in raw or "@" in raw:
        raise err
    try:
        return raw.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError, UnicodeEncodeError):
        raise err from None


def _normalize_address(address: str, *, as_config: bool = False) -> Tuple[str, str]:
    """Return (normalized_full_address, normalized_domain)."""
    addr = (address or "").strip()
    reject = (
        InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID")
        if as_config
        else InternalEmailError(403, "INTERNAL_EMAIL_RECIPIENT_NOT_ALLOWED")
    )
    if "@" not in addr or " " in addr or "\n" in addr or "\r" in addr:
        raise reject
    local, _, domain = addr.partition("@")
    if not local or not domain:
        raise reject
    norm_domain = _normalize_domain(domain, as_config=as_config)
    return f"{local.lower()}@{norm_domain}", norm_domain


def _parse_address_list(raw: Optional[str]) -> Set[str]:
    if raw is None:
        return set()
    out: Set[str] = set()
    for piece in str(raw).split(","):
        token = piece.strip()
        if not token:
            continue
        try:
            normalized, _ = _normalize_address(token, as_config=True)
        except InternalEmailError:
            raise InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID") from None
        out.add(normalized)
    return out


def _parse_domain_list(raw: Optional[str]) -> Set[str]:
    if raw is None:
        return set()
    out: Set[str] = set()
    for piece in str(raw).split(","):
        token = piece.strip().lstrip("@")
        if not token:
            continue
        if "*" in token or token.startswith("."):
            raise InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID")
        out.add(_normalize_domain(token, as_config=True))
    return out


def _load_sender() -> str:
    configured = (os.getenv("INTERNAL_EMAIL_FROM") or "").strip()
    sender = configured or _DEFAULT_FROM
    if sender not in _SUPPORTED_FROM:
        raise InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID")
    return sender


def _load_recipient_policy() -> Tuple[Set[str], Set[str]]:
    allowed_addrs = _parse_address_list(os.getenv("INTERNAL_EMAIL_ALLOWED_RECIPIENTS"))
    allowed_domains = _parse_domain_list(os.getenv("INTERNAL_EMAIL_ALLOWED_DOMAINS"))
    if not allowed_addrs and not allowed_domains:
        raise InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID")
    return allowed_addrs, allowed_domains


def _assert_recipient_allowed(
    address: str,
    allowed_addrs: Set[str],
    allowed_domains: Set[str],
) -> Tuple[str, str]:
    normalized, domain = _normalize_address(address)
    if normalized in allowed_addrs or domain in allowed_domains:
        return normalized, domain
    raise InternalEmailError(403, "INTERNAL_EMAIL_RECIPIENT_NOT_ALLOWED")


def _validate_subject(raw: str) -> str:
    value = str(raw if raw is not None else "")
    if "\x00" in value or _SUBJECT_CONTROL_RE.search(value):
        raise InternalEmailError(422, "INVALID_SUBJECT")
    cleaned = value.strip()
    if not cleaned:
        raise InternalEmailError(422, "INVALID_SUBJECT")
    if len(cleaned) > _SUBJECT_MAX:
        raise InternalEmailError(422, "INVALID_SUBJECT")
    return cleaned


def _validate_message(raw: str) -> str:
    value = str(raw if raw is not None else "")
    if "\x00" in value or _MESSAGE_DISALLOWED_CONTROL_RE.search(value):
        raise InternalEmailError(422, "INVALID_MESSAGE")
    # Trim outer whitespace; preserve internal line breaks.
    cleaned = value.strip(" \t\r\n")
    if not cleaned:
        raise InternalEmailError(422, "INVALID_MESSAGE")
    if len(cleaned) > _MESSAGE_MAX:
        raise InternalEmailError(422, "INVALID_MESSAGE")
    return cleaned


def _require_live_unlocked_caller(db: Session, jwt_sub: str) -> User:
    caller_id = str(jwt_sub or "").strip()
    if not caller_id:
        raise InternalEmailError(401, "Not authenticated")
    user = db.query(User).filter(User.userAppId == caller_id).first()
    if user is None:
        raise InternalEmailError(404, "USER_NOT_FOUND")
    if _is_tombstone_user_app_id(getattr(user, "userAppId", None)):
        raise InternalEmailError(404, "USER_NOT_FOUND")
    if bool(getattr(user, "lockApp", False)):
        raise InternalEmailError(403, "ACCOUNT_LOCKED")
    return user


def _apply_rate_limits(
    db: Session,
    *,
    caller_id: str,
    recipient: str,
    domain: str,
    subject: str,
    message: str,
) -> None:
    caller_h = _hash_part(caller_id)
    recip_h = _hash_part(recipient)
    domain_h = _hash_part(domain)

    checks = [
        (f"internal_email:caller_min:{caller_h}", _CALLER_PER_MIN, 60),
        (f"internal_email:caller_hour:{caller_h}", _CALLER_PER_HOUR, 3600),
        (f"internal_email:caller_day:{caller_h}", _CALLER_PER_DAY, 86400),
        (f"internal_email:recipient_hour:{recip_h}", _RECIPIENT_PER_HOUR, 3600),
        (f"internal_email:domain_hour:{domain_h}", _DOMAIN_PER_HOUR, 3600),
    ]
    for key, max_hits, window in checks:
        limited = enforce_rate_limit(
            db,
            bucket_key=key,
            max_hits=max_hits,
            window_seconds=window,
            fail_closed=True,
        )
        if limited is not None:
            raise InternalEmailError(429, "INTERNAL_EMAIL_RATE_LIMITED")

    body_digest = _hash_part(f"{subject}\n{message}")
    dup_key = (
        f"internal_email:dup:{caller_h}:{recip_h}:{_hash_part(subject)}:{body_digest}"
    )
    dup_limited = enforce_rate_limit(
        db,
        bucket_key=dup_key,
        max_hits=1,
        window_seconds=_DUP_WINDOW_SECONDS,
        fail_closed=True,
    )
    if dup_limited is not None:
        raise InternalEmailError(429, "INTERNAL_EMAIL_DUPLICATE_SUPPRESSED")


def send_internal_email(
    db: Session,
    *,
    jwt_sub: str,
    request: InternalEmailSendRequest,
) -> Dict[str, str]:
    """
    Validate caller + payload, enforce policy/rate limits, send plain-text mail.

    Returns ``{"message": "SENT"}`` on SMTP acceptance.
    """
    started = time.monotonic()
    purpose_value = (
        request.purpose.value
        if isinstance(request.purpose, InternalEmailPurpose)
        else str(request.purpose)
    )
    caller_id: Optional[str] = str(jwt_sub or "").strip() or None
    recipient_domain: Optional[str] = None
    recipient_hash: Optional[str] = None
    subject_len: Optional[int] = None
    body_len: Optional[int] = None

    def _fail(err: InternalEmailError) -> None:
        _audit(
            outcome=err.detail,
            caller_id=caller_id,
            purpose=purpose_value,
            recipient_domain=recipient_domain,
            recipient_hash=recipient_hash,
            subject_len=subject_len,
            body_len=body_len,
            latency_ms=(time.monotonic() - started) * 1000,
        )
        raise err

    try:
        user = _require_live_unlocked_caller(db, jwt_sub)
        caller_id = str(user.userAppId)

        subject = _validate_subject(request.subject)
        message = _validate_message(request.message)
        subject_len = len(subject)
        body_len = len(message)

        allowed_addrs, allowed_domains = _load_recipient_policy()
        recipient, recipient_domain = _assert_recipient_allowed(
            str(request.toAddress),
            allowed_addrs,
            allowed_domains,
        )
        recipient_hash = _hash_part(recipient)

        from_address = _load_sender()

        _apply_rate_limits(
            db,
            caller_id=caller_id,
            recipient=recipient,
            domain=recipient_domain,
            subject=subject,
            message=message,
        )
    except InternalEmailError as err:
        _fail(err)

    used_fallback: Optional[bool] = None
    try:
        result = send_email(
            message=message,
            subject=subject,
            from_address=from_address,
            from_name="OpenBid Internal",
            to_address=recipient,
            to_name="Recipient",
            cc_address=None,
            cc_name=None,
            bcc_address=None,
            bcc_name=None,
            attachment_path=None,
            is_html=False,
        )
    except Exception as exc:
        _logger.warning(
            "internal_email_smtp_exception exc_class=%s caller_hash=%s purpose=%s",
            type(exc).__name__,
            _hash_part(caller_id or ""),
            purpose_value,
        )
        _audit(
            outcome="INTERNAL_EMAIL_DELIVERY_FAILED",
            caller_id=caller_id,
            purpose=purpose_value,
            recipient_domain=recipient_domain,
            recipient_hash=recipient_hash,
            subject_len=subject_len,
            body_len=body_len,
            latency_ms=(time.monotonic() - started) * 1000,
        )
        raise InternalEmailError(503, "INTERNAL_EMAIL_DELIVERY_FAILED") from None

    message_code = str((result or {}).get("message") or "")
    used_fallback = bool((result or {}).get("used_fallback"))

    if message_code == "SENT":
        _audit(
            outcome="SENT",
            caller_id=caller_id,
            purpose=purpose_value,
            recipient_domain=recipient_domain,
            recipient_hash=recipient_hash,
            subject_len=subject_len,
            body_len=body_len,
            latency_ms=(time.monotonic() - started) * 1000,
            used_fallback=used_fallback,
        )
        return {"message": "SENT"}

    if message_code == "ERROR_INVALID_FROMADDRESS":
        _fail(InternalEmailError(503, "INTERNAL_EMAIL_CONFIGURATION_INVALID"))

    _audit(
        outcome="INTERNAL_EMAIL_DELIVERY_FAILED",
        caller_id=caller_id,
        purpose=purpose_value,
        recipient_domain=recipient_domain,
        recipient_hash=recipient_hash,
        subject_len=subject_len,
        body_len=body_len,
        latency_ms=(time.monotonic() - started) * 1000,
        used_fallback=used_fallback,
    )
    raise InternalEmailError(503, "INTERNAL_EMAIL_DELIVERY_FAILED")
