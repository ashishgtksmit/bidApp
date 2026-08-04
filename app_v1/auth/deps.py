"""HTTP auth dependencies (PR37 session + PR38 immutable subject)."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth.jwt import (
    IDENTITY_VERSION_IMMUTABLE,
    IDENTITY_VERSION_LEGACY_PHONE,
    allow_legacy_phone_sub,
    decode_token,
)
from ..models.user_table import User

http_bearer = HTTPBearer(auto_error=True, scheme_name="BearerAuth")
client_id_scheme = APIKeyHeader(
    name="X-Client-Id", auto_error=False, scheme_name="ClientIdHeader"
)

_logger = logging.getLogger("openbid.auth.pr38")

# Safe in-process counters (never include phone/token/authSubjectId raw values).
_METRIC_LEGACY_ACCESS_ACCEPTED = 0
_METRIC_LEGACY_REFRESH_CONVERTED = 0
_METRIC_LEGACY_REJECTED_AFTER_DISABLE = 0


@dataclass(frozen=True)
class AuthenticatedUser:
    """Typed authentication identity resolved from a validated JWT."""

    uid: int
    auth_subject: str
    user_app_id: str
    account_session_id: str
    session_version: int
    roles: tuple[str, ...]
    identity_version: int


def _is_tombstone_app_id(user_app_id: str | None) -> bool:
    app_id = str(user_app_id or "")
    return ".DELETED" in app_id.upper()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )


def _session_invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="SESSION_INVALID",
    )


def hash_auth_subject_for_limit(auth_subject: str) -> str:
    """Hashed auth subject for rate-limit / audit keys (never raw)."""
    return hashlib.sha256(str(auth_subject).encode("utf-8")).hexdigest()[:32]


def record_legacy_access_accepted() -> None:
    global _METRIC_LEGACY_ACCESS_ACCEPTED
    _METRIC_LEGACY_ACCESS_ACCEPTED += 1
    _logger.info(
        "pr38_metric event=legacy_access_accepted count=%s",
        _METRIC_LEGACY_ACCESS_ACCEPTED,
    )


def record_legacy_refresh_converted() -> None:
    global _METRIC_LEGACY_REFRESH_CONVERTED
    _METRIC_LEGACY_REFRESH_CONVERTED += 1
    _logger.info(
        "pr38_metric event=legacy_refresh_converted count=%s",
        _METRIC_LEGACY_REFRESH_CONVERTED,
    )


def record_legacy_rejected_after_disable() -> None:
    global _METRIC_LEGACY_REJECTED_AFTER_DISABLE
    _METRIC_LEGACY_REJECTED_AFTER_DISABLE += 1
    _logger.info(
        "pr38_metric event=legacy_token_rejected_after_disable count=%s",
        _METRIC_LEGACY_REJECTED_AFTER_DISABLE,
    )


def _roles_tuple(user: User, payload: dict) -> tuple[str, ...]:
    claim_roles = payload.get("roles")
    if isinstance(claim_roles, list) and claim_roles:
        return tuple(str(r) for r in claim_roles)
    return ("vendor",) if bool(getattr(user, "alsoVendor", False)) else ("user",)


def _verify_session_claims(user: User, payload: dict) -> None:
    token_sv = payload.get("session_version")
    token_sid = payload.get("session_id")
    if token_sv is None or not token_sid:
        raise _unauthorized()
    try:
        row_sv = int(user.sessionVersion)
        claim_sv = int(token_sv)
    except (TypeError, ValueError) as exc:
        raise _unauthorized() from exc
    if claim_sv != row_sv:
        raise _unauthorized()
    if str(token_sid) != str(user.accountSessionId):
        raise _unauthorized()


def _user_to_authenticated(
    user: User,
    *,
    payload: dict,
    identity_version: int,
) -> AuthenticatedUser:
    if user is None or _is_tombstone_app_id(user.userAppId):
        raise _unauthorized()
    auth_subject = str(getattr(user, "authSubjectId", "") or "").strip()
    if not auth_subject:
        raise _unauthorized()
    _verify_session_claims(user, payload)
    return AuthenticatedUser(
        uid=int(user.UID),
        auth_subject=auth_subject,
        user_app_id=str(user.userAppId),
        account_session_id=str(user.accountSessionId),
        session_version=int(user.sessionVersion),
        roles=_roles_tuple(user, payload),
        identity_version=int(identity_version),
    )


def _infer_legacy_identity_version(payload: dict) -> Optional[int]:
    """Return 1 for PR37-compatible tokens; None if not eligible for legacy path."""
    raw = payload.get("identity_version")
    if raw is None:
        # Missing identity_version may be PR37 if full session claims exist.
        if (
            payload.get("session_version") is not None
            and payload.get("session_id")
            and payload.get("sub")
            and payload.get("type") in {"access", "refresh"}
        ):
            return IDENTITY_VERSION_LEGACY_PHONE
        return None
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return None
    return version


def resolve_token_user(
    db: Session,
    claims: dict,
    *,
    expected_type: str,
    allow_legacy: Optional[bool] = None,
    raise_session_invalid: bool = False,
) -> AuthenticatedUser:
    """Resolve JWT claims to AuthenticatedUser (PR38 or permitted PR37).

    Never returns raw token ``sub`` as phone. Callers must use
    ``AuthenticatedUser.user_app_id`` for business ownership columns.
    """
    fail = _session_invalid if raise_session_invalid else _unauthorized

    if claims.get("type") != expected_type:
        raise fail()

    sub = claims.get("sub")
    if not sub or not str(sub).strip():
        raise fail()
    sub = str(sub).strip()

    version = _infer_legacy_identity_version(claims)
    if version is None:
        raise fail()

    legacy_allowed = (
        allow_legacy_phone_sub() if allow_legacy is None else bool(allow_legacy)
    )

    if version == IDENTITY_VERSION_IMMUTABLE:
        user = (
            db.query(User).filter(User.authSubjectId == sub).first()
        )
        if user is None or _is_tombstone_app_id(getattr(user, "userAppId", None)):
            raise fail()
        return _user_to_authenticated(
            user, payload=claims, identity_version=IDENTITY_VERSION_IMMUTABLE
        )

    if version == IDENTITY_VERSION_LEGACY_PHONE:
        if not legacy_allowed:
            record_legacy_rejected_after_disable()
            raise fail()
        # Require full PR37 session claims for phone-sub acceptance.
        if claims.get("session_version") is None or not claims.get("session_id"):
            raise fail()
        user = db.query(User).filter(User.userAppId == sub).first()
        if user is None or _is_tombstone_app_id(getattr(user, "userAppId", None)):
            raise fail()
        authenticated = _user_to_authenticated(
            user, payload=claims, identity_version=IDENTITY_VERSION_LEGACY_PHONE
        )
        if expected_type == "access":
            record_legacy_access_accepted()
        return authenticated

    # Unknown / unsupported identity version (including attempted downgrade misuse)
    raise fail()


def validate_access_session(
    db: Session,
    *,
    payload: dict,
) -> AuthenticatedUser:
    """Validate access-token session claims; return typed AuthenticatedUser.

    Backward-compatible name retained for ws-validate and tests. Return type
    changed from phone ``str`` to ``AuthenticatedUser`` in PR38.
    """
    return resolve_token_user(
        db,
        payload,
        expected_type="access",
        allow_legacy=None,
        raise_session_invalid=False,
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(http_bearer),
    db: Session = Depends(get_db),
    x_client_id: str | None = Security(client_id_scheme),
) -> AuthenticatedUser:
    """Primary authenticated dependency — returns typed AuthenticatedUser."""
    try:
        token = credentials.credentials
        payload = decode_token(db=db, token=token, client_id=x_client_id)
        return validate_access_session(db, payload=payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _unauthorized() from exc


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Security(http_bearer),
    db: Session = Depends(get_db),
    x_client_id: str | None = Security(client_id_scheme),
) -> str:
    """Deprecated: returns ``AuthenticatedUser.user_app_id`` (current phone).

    Do not use for new endpoints. Prefer :func:`get_current_user`.
    Never returns raw JWT ``sub`` for version-2 tokens.
    """
    user = get_current_user(
        credentials=credentials, db=db, x_client_id=x_client_id
    )
    return user.user_app_id
