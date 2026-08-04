"""JWT create/decode helpers (PR37 session identity + PR38 immutable subject)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
from sqlalchemy.orm import Session

from ..models.client_secrets import ClientSecret

# PR38 identity versions
IDENTITY_VERSION_LEGACY_PHONE = 1
IDENTITY_VERSION_IMMUTABLE = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# env
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 15)
REFRESH_TOKEN_EXPIRE_DAYS = _env_int("REFRESH_TOKEN_EXPIRE_DAYS", 30)
JWT_ISSUER = os.getenv("JWT_ISSUER")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE")
JWT_CLOCK_LEEWAY_SECONDS = _env_int("JWT_CLOCK_LEEWAY_SECONDS", 30)

# PR38 — compatibility + mint switches (defaults documented in .env.example / README)
# Default True: accept valid PR37 phone-sub tokens during compatibility window.
JWT_ALLOW_LEGACY_PHONE_SUB = _env_bool("JWT_ALLOW_LEGACY_PHONE_SUB", True)
# Default 2: mint immutable-authSubjectId tokens. Set to 1 only for emergency rollback.
JWT_IDENTITY_VERSION_TO_MINT = _env_int("JWT_IDENTITY_VERSION_TO_MINT", 2)


def allow_legacy_phone_sub() -> bool:
    """Fail-safe: when False, phone-sub access/refresh → SESSION_INVALID."""
    return _env_bool("JWT_ALLOW_LEGACY_PHONE_SUB", True)


def identity_version_to_mint() -> int:
    """Mint version for new login/refresh pairs. Unsupported values fall back to 2."""
    value = _env_int("JWT_IDENTITY_VERSION_TO_MINT", 2)
    if value not in (IDENTITY_VERSION_LEGACY_PHONE, IDENTITY_VERSION_IMMUTABLE):
        return IDENTITY_VERSION_IMMUTABLE
    return value


def get_signing_secret(db: Session, client_id: Optional[str]) -> str:
    if not client_id:
        return JWT_SECRET
    cs = (
        db.query(ClientSecret)
        .filter(
            ClientSecret.clientId == client_id,
            ClientSecret.isActive == True,  # noqa: E712
        )
        .first()
    )
    return cs.secretKey if cs and cs.secretKey else JWT_SECRET


def _new_jti() -> str:
    return uuid.uuid4().hex


def _base_claims(
    *,
    auth_subject: str,
    identity_version: int,
    token_type: str,
    session_version: int,
    account_session_id: str,
    exp: datetime,
    now: datetime,
) -> Dict[str, Any]:
    aud = JWT_AUDIENCE.strip() if JWT_AUDIENCE else None
    payload: Dict[str, Any] = {
        "sub": auth_subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": JWT_ISSUER,
        "aud": aud,
        "jti": _new_jti(),
        "session_version": int(session_version),
        "session_id": str(account_session_id),
        "identity_version": int(identity_version),
    }
    return payload


def create_access_token(
    *,
    db: Session,
    auth_subject: str,
    identity_version: int,
    session_version: int,
    account_session_id: str,
    client_id: Optional[str],
    roles: Optional[List[str]] = None,
) -> str:
    """Mint an access JWT with PR37 session claims + PR38 identity_version.

    ``auth_subject`` must be the opaque ``User.authSubjectId`` for version 2,
    or phone/userAppId only when intentionally minting version 1 (rollback).
    Do not pass phone as subject for version-2 tokens.
    """
    if not auth_subject or not str(auth_subject).strip():
        raise ValueError("auth_subject required")
    if session_version is None or not account_session_id:
        raise ValueError("session claims required")
    if identity_version not in (
        IDENTITY_VERSION_LEGACY_PHONE,
        IDENTITY_VERSION_IMMUTABLE,
    ):
        raise ValueError("unsupported identity_version")
    now = _now()
    exp = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = _base_claims(
        auth_subject=str(auth_subject).strip(),
        identity_version=int(identity_version),
        token_type="access",
        session_version=session_version,
        account_session_id=account_session_id,
        exp=exp,
        now=now,
    )
    if roles is not None:
        payload["roles"] = roles
    secret = get_signing_secret(db, client_id)
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def create_refresh_token(
    *,
    db: Session,
    auth_subject: str,
    identity_version: int,
    session_version: int,
    account_session_id: str,
    client_id: Optional[str],
) -> str:
    """Mint a refresh JWT with PR37 session claims + PR38 identity_version.

    Refresh lifetime uses days (REFRESH_TOKEN_EXPIRE_DAYS), not minutes.
    """
    if not auth_subject or not str(auth_subject).strip():
        raise ValueError("auth_subject required")
    if session_version is None or not account_session_id:
        raise ValueError("session claims required")
    if identity_version not in (
        IDENTITY_VERSION_LEGACY_PHONE,
        IDENTITY_VERSION_IMMUTABLE,
    ):
        raise ValueError("unsupported identity_version")
    now = _now()
    exp = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = _base_claims(
        auth_subject=str(auth_subject).strip(),
        identity_version=int(identity_version),
        token_type="refresh",
        session_version=session_version,
        account_session_id=account_session_id,
        exp=exp,
        now=now,
    )
    secret = get_signing_secret(db, client_id)
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_token(
    *,
    db: Session,
    token: str,
    client_id: Optional[str],
    verify_aud: bool = True,
) -> Dict[str, Any]:
    """Decode and validate a JWT with issuer/audience/algorithm/leeway."""
    secret = get_signing_secret(db, client_id)
    aud = JWT_AUDIENCE.strip() if JWT_AUDIENCE else None
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=aud if verify_aud else None,
            issuer=JWT_ISSUER,
            options={
                "verify_aud": verify_aud,
                "leeway": JWT_CLOCK_LEEWAY_SECONDS,
            },
        )
    except ExpiredSignatureError as e:
        raise ValueError("expired") from e
    except JWTClaimsError as e:
        raise ValueError(str(e)) from e
    except JWTError as e:
        raise ValueError(str(e)) from e
