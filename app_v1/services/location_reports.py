"""PR29 — authenticated missing-location report orchestration.

JWT-owned reporter identity, server-owned email template/recipients, synchronous
SMTP via utils.email.send_email. Does not mutate location/region catalog tables.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..models.region_details import Region
from ..models.user_table import User
from ..utils.email import send_email
from ..utils.rate_limit import enforce_rate_limit

_logger = logging.getLogger(__name__)

_LOCATION_MIN = 2
_LOCATION_MAX = 120
_LANDMARK_MIN = 2
_LANDMARK_MAX = 250

_SENDER_MAX = int(os.getenv("RATE_LIMIT_LOCATION_REPORT_SENDER_PER_HOUR", "5"))
_SENDER_WINDOW = int(
    os.getenv("RATE_LIMIT_LOCATION_REPORT_SENDER_WINDOW_SECONDS", "3600")
)
_LOCATION_MAX_HITS = int(
    os.getenv("RATE_LIMIT_LOCATION_REPORT_SAME_LOCATION_PER_DAY", "1")
)
_LOCATION_WINDOW = int(
    os.getenv("RATE_LIMIT_LOCATION_REPORT_SAME_LOCATION_WINDOW_SECONDS", "86400")
)

_DEFAULT_FROM = "customersupport@wizzride.com"
_SUPPORTED_FROM = frozenset(
    {
        "reservations@wizzride.com",
        "customersupport@wizzride.com",
    }
)

# Control chars, null, CR/LF; HTML/script indicators.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_SCRIPT_RE = re.compile(r"(?i)<\s*script|javascript:|on\w+\s*=")


class LocationReportError(Exception):
    """Typed location-report failure with HTTP status + stable detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _hash_part(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def _is_tombstone_user_app_id(user_app_id: Optional[str]) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_for_bucket(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _validate_free_text(value: str, *, field: str, min_len: int, max_len: int) -> str:
    raw = str(value if value is not None else "")
    # Reject control/CRLF/null before whitespace normalization collapses them.
    if "\x00" in raw or _CONTROL_RE.search(raw):
        raise LocationReportError(422, f"INVALID_{field.upper()}")
    cleaned = _normalize_text(raw)
    if not cleaned:
        raise LocationReportError(422, f"INVALID_{field.upper()}")
    if len(cleaned) < min_len or len(cleaned) > max_len:
        raise LocationReportError(422, f"INVALID_{field.upper()}")
    if _HTML_TAG_RE.search(cleaned) or _SCRIPT_RE.search(cleaned):
        raise LocationReportError(422, f"INVALID_{field.upper()}")
    return cleaned


def _load_user(db: Session, user_app_id: str) -> Optional[User]:
    return db.query(User).filter(User.userAppId == user_app_id).first()


def _require_live_unlocked_reporter(db: Session, jwt_sub: str) -> User:
    reporter_id = str(jwt_sub or "").strip()
    if not reporter_id:
        raise LocationReportError(401, "Not authenticated")
    user = _load_user(db, reporter_id)
    if user is None:
        raise LocationReportError(404, "USER_NOT_FOUND")
    if _is_tombstone_user_app_id(getattr(user, "userAppId", None)):
        raise LocationReportError(404, "USER_NOT_FOUND")
    if bool(getattr(user, "lockApp", False)):
        raise LocationReportError(403, "ACCOUNT_LOCKED")
    return user


def _resolve_region(
    db: Session,
    *,
    region_id: Optional[int],
    region_other: bool,
) -> Tuple[Optional[int], str]:
    if region_other:
        if region_id is not None:
            raise LocationReportError(422, "INVALID_REGION")
        return None, "Others"
    if region_id is None:
        raise LocationReportError(422, "INVALID_REGION")
    region = db.query(Region).filter(Region.RDID == region_id).first()
    if region is None:
        raise LocationReportError(404, "REGION_NOT_FOUND")
    name = _normalize_text(str(getattr(region, "regionName", "") or ""))
    if not name:
        raise LocationReportError(404, "REGION_NOT_FOUND")
    return int(region.RDID), name


def _parse_email_list(raw: Optional[str]) -> list[str]:
    if raw is None:
        return []
    parts = []
    for piece in str(raw).split(","):
        addr = piece.strip()
        if not addr:
            continue
        if "@" not in addr or " " in addr or "\n" in addr or "\r" in addr:
            raise LocationReportError(503, "LOCATION_REPORT_CONFIGURATION_INVALID")
        parts.append(addr)
    return parts


def _load_mail_config() -> Dict[str, Any]:
    to_list = _parse_email_list(os.getenv("LOCATION_REPORT_EMAIL_TO"))
    if not to_list:
        raise LocationReportError(503, "LOCATION_REPORT_CONFIGURATION_INVALID")

    from_raw = (os.getenv("LOCATION_REPORT_EMAIL_FROM") or "").strip()
    from_address = from_raw or _DEFAULT_FROM
    if from_address not in _SUPPORTED_FROM:
        raise LocationReportError(503, "LOCATION_REPORT_CONFIGURATION_INVALID")

    cc_list = _parse_email_list(os.getenv("LOCATION_REPORT_EMAIL_CC"))
    bcc_list = _parse_email_list(os.getenv("LOCATION_REPORT_EMAIL_BCC"))

    return {
        "to_address": to_list[0],
        "to_extra": to_list[1:],
        "from_address": from_address,
        "cc_address": ", ".join(cc_list) if cc_list else None,
        "bcc_address": ", ".join(bcc_list) if bcc_list else None,
    }


def _usage_label(usage_type: str) -> str:
    return "Pickup" if usage_type == "PICKUP" else "Drop"


def _build_subject(usage_type: str) -> str:
    return f"OpenBid Missing Location Report — {_usage_label(usage_type)}"


def _build_html(
    *,
    location_name: str,
    landmark: str,
    region_name: str,
    usage_type: str,
    reporter_name: str,
    reporter_phone: str,
    reporter_email: Optional[str],
) -> str:
    esc = html.escape
    submitted_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
    email_row = (
        f"<tr><td>Reporter email</td><td>{esc(reporter_email)}</td></tr>"
        if reporter_email
        else ""
    )
    return f"""<!DOCTYPE html>
<html><body>
<p>OpenBid missing location report</p>
<table>
<tr><td>Requested location</td><td>{esc(location_name)}</td></tr>
<tr><td>Landmark</td><td>{esc(landmark)}</td></tr>
<tr><td>Region</td><td>{esc(region_name)}</td></tr>
<tr><td>Usage type</td><td>{esc(_usage_label(usage_type))}</td></tr>
<tr><td>Reporter name</td><td>{esc(reporter_name)}</td></tr>
<tr><td>Reporter phone</td><td>{esc(reporter_phone)}</td></tr>
{email_row}
<tr><td>Submitted at (Asia/Kolkata)</td><td>{esc(submitted_at)}</td></tr>
<tr><td>Source</td><td>OpenBid mobile application</td></tr>
</table>
</body></html>
"""


def _apply_rate_limits(
    db: Session,
    *,
    reporter_id: str,
    location_name: str,
    region_name: str,
    usage_type: str,
) -> None:
    sender_limited = enforce_rate_limit(
        db,
        bucket_key=f"location_report:sender:{_hash_part(reporter_id)}",
        max_hits=_SENDER_MAX,
        window_seconds=_SENDER_WINDOW,
    )
    if sender_limited is not None:
        raise LocationReportError(429, "LOCATION_REPORT_RATE_LIMITED")

    loc_key = (
        f"location_report:loc:{_hash_part(reporter_id)}:"
        f"{_hash_part(_normalize_for_bucket(location_name))}:"
        f"{_hash_part(_normalize_for_bucket(region_name))}:"
        f"{usage_type}"
    )
    loc_limited = enforce_rate_limit(
        db,
        bucket_key=loc_key,
        max_hits=_LOCATION_MAX_HITS,
        window_seconds=_LOCATION_WINDOW,
    )
    if loc_limited is not None:
        raise LocationReportError(429, "LOCATION_REPORT_RATE_LIMITED")


def submit_location_report(
    db: Session,
    *,
    jwt_sub: str,
    location_name: str,
    landmark: str,
    region_id: Optional[int],
    region_other: bool,
    usage_type: str,
) -> Dict[str, str]:
    """
    Validate reporter + region, rate-limit, send server-owned operational email.

    Returns ``{"message": "REPORT_SUBMITTED"}`` only after SMTP acceptance.
    Does not insert into catalog tables. Does not use BackgroundTasks.
    """
    if usage_type not in ("PICKUP", "DROP"):
        raise LocationReportError(422, "INVALID_USAGE_TYPE")

    reporter = _require_live_unlocked_reporter(db, jwt_sub)
    location_clean = _validate_free_text(
        location_name,
        field="locationName",
        min_len=_LOCATION_MIN,
        max_len=_LOCATION_MAX,
    )
    landmark_clean = _validate_free_text(
        landmark,
        field="landmark",
        min_len=_LANDMARK_MIN,
        max_len=_LANDMARK_MAX,
    )
    resolved_region_id, region_name = _resolve_region(
        db, region_id=region_id, region_other=region_other
    )

    mail_cfg = _load_mail_config()

    _apply_rate_limits(
        db,
        reporter_id=str(reporter.userAppId),
        location_name=location_clean,
        region_name=region_name,
        usage_type=usage_type,
    )

    reporter_name = _normalize_text(str(getattr(reporter, "fullName", "") or "")) or "—"
    reporter_phone = str(getattr(reporter, "userAppId", "") or "")
    reporter_email_raw = _normalize_text(str(getattr(reporter, "emailId", "") or ""))
    reporter_email = reporter_email_raw or None

    subject = _build_subject(usage_type)
    body = _build_html(
        location_name=location_clean,
        landmark=landmark_clean,
        region_name=region_name,
        usage_type=usage_type,
        reporter_name=reporter_name,
        reporter_phone=reporter_phone,
        reporter_email=reporter_email,
    )

    # Extra configured TO recipients ride along via CC so send_email stays single-TO.
    cc_parts = []
    if mail_cfg["to_extra"]:
        cc_parts.extend(mail_cfg["to_extra"])
    if mail_cfg["cc_address"]:
        cc_parts.append(mail_cfg["cc_address"])
    cc_combined = ", ".join(cc_parts) if cc_parts else None

    try:
        result = send_email(
            message=body,
            subject=subject,
            from_address=mail_cfg["from_address"],
            from_name="OpenBid App",
            to_address=mail_cfg["to_address"],
            to_name="OpenBid Support",
            cc_address=cc_combined,
            cc_name=None,
            bcc_address=mail_cfg["bcc_address"],
            bcc_name=None,
        )
    except Exception as exc:
        _logger.warning(
            "location_report_smtp_exception category=%s usage=%s region_other=%s "
            "region_id=%s reporter_hash=%s exc_class=%s",
            "smtp_exception",
            usage_type,
            region_other,
            resolved_region_id,
            _hash_part(str(reporter.userAppId)),
            type(exc).__name__,
        )
        raise LocationReportError(503, "LOCATION_REPORT_DELIVERY_FAILED") from None

    message = str((result or {}).get("message") or "")
    if message == "SENT":
        _logger.info(
            "location_report_submitted usage=%s region_other=%s region_id=%s "
            "reporter_hash=%s location_len=%s landmark_len=%s",
            usage_type,
            region_other,
            resolved_region_id,
            _hash_part(str(reporter.userAppId)),
            len(location_clean),
            len(landmark_clean),
        )
        return {"message": "REPORT_SUBMITTED"}

    if message == "ERROR_INVALID_FROMADDRESS":
        raise LocationReportError(503, "LOCATION_REPORT_CONFIGURATION_INVALID")

    _logger.warning(
        "location_report_delivery_failed category=%s usage=%s region_other=%s "
        "region_id=%s reporter_hash=%s result_code=%s",
        "smtp_result",
        usage_type,
        region_other,
        resolved_region_id,
        _hash_part(str(reporter.userAppId)),
        message,
    )
    raise LocationReportError(503, "LOCATION_REPORT_DELIVERY_FAILED")
