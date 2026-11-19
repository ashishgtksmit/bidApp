"""Admin authentication: password hashing, JWT, role checks, audit helper."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..database import get_db
from .models import ROLES, AdminAuditLog, AdminUser

ADMIN_AUDIENCE = "openbid-admin"
ADMIN_ISSUER = "openbid-api"
ACCESS_TTL_HOURS = 8
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

# Strict: no plaintext fallback (unlike utils.security.verify_password).
_pwd = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")

_bearer = HTTPBearer(auto_error=False, scheme_name="AdminBearer")

# What each role may do. "read" is implied for every role.
PERMISSIONS: dict[str, set[str]] = {
    "super_admin": {"*"},
    "operations": {
        "partners.write", "fleet.write", "marketplace.write", "catalog.write",
        "notifications.send", "accounts.lock", "pii.read", "export",
    },
    "finance": {"money.write", "subscriptions.write", "pii.read", "export"},
    "support": {"accounts.lock", "notifications.send", "pii.read"},
    "viewer": set(),
}


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bool(_pwd.verify(plain, hashed))
    except Exception:
        return False


def validate_new_password(plain: str) -> None:
    if len(plain) < 12 or plain.lower() == plain or plain.upper() == plain or not any(c.isdigit() for c in plain):
        raise HTTPException(
            status_code=422,
            detail="PASSWORD_TOO_WEAK",
        )


def _secret() -> str:
    secret = (os.getenv("ADMIN_JWT_SECRET") or "").strip()
    if len(secret) < 32:
        # Fail closed: never sign admin tokens with a weak / missing secret.
        raise HTTPException(status_code=503, detail="ADMIN_AUTH_NOT_CONFIGURED")
    return secret


def issue_token(admin: AdminUser) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=ACCESS_TTL_HOURS)
    payload = {
        "sub": str(admin.id),
        "aud": ADMIN_AUDIENCE,
        "iss": ADMIN_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "role": admin.role,
        "tv": int(admin.token_version),
        "type": "admin_access",
    }
    return jwt.encode(payload, _secret(), algorithm="HS256"), exp


@dataclass(frozen=True)
class CurrentAdmin:
    id: int
    email: str
    full_name: str
    role: str
    must_change_password: bool

    def can(self, permission: str) -> bool:
        perms = PERMISSIONS.get(self.role, set())
        return "*" in perms or permission in perms


def _unauthorized(detail: str = "ADMIN_AUTH_REQUIRED") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_admin(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> CurrentAdmin:
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        claims = jwt.decode(
            creds.credentials,
            _secret(),
            algorithms=["HS256"],
            audience=ADMIN_AUDIENCE,
            issuer=ADMIN_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("ADMIN_SESSION_EXPIRED")
    except jwt.PyJWTError:
        raise _unauthorized()
    if claims.get("type") != "admin_access":
        raise _unauthorized()
    try:
        admin_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise _unauthorized()
    admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if admin is None or not admin.is_active or int(claims.get("tv", -1)) != int(admin.token_version):
        raise _unauthorized("ADMIN_SESSION_INVALID")
    if admin.role not in ROLES:
        raise _unauthorized("ADMIN_SESSION_INVALID")
    return CurrentAdmin(
        id=admin.id,
        email=admin.email,
        full_name=admin.full_name,
        role=admin.role,
        must_change_password=bool(admin.must_change_password),
    )


def require(permission: str) -> Callable[..., CurrentAdmin]:
    """Dependency: the signed-in admin must hold ``permission``."""

    def _dep(admin: CurrentAdmin = Depends(get_current_admin)) -> CurrentAdmin:
        if admin.must_change_password:
            raise HTTPException(status_code=403, detail="PASSWORD_CHANGE_REQUIRED")
        if not admin.can(permission):
            raise HTTPException(status_code=403, detail="ADMIN_PERMISSION_DENIED")
        return admin

    return _dep


def require_reader(admin: CurrentAdmin = Depends(get_current_admin)) -> CurrentAdmin:
    if admin.must_change_password:
        raise HTTPException(status_code=403, detail="PASSWORD_CHANGE_REQUIRED")
    return admin


def _jsonable(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, default=str, sort_keys=True)[:20000]


def audit(
    db: Session,
    admin: CurrentAdmin,
    request: Optional[Request],
    *,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    summary: Optional[str] = None,
    before: Any = None,
    after: Any = None,
    commit: bool = False,
) -> None:
    """Append an audit row in the caller's transaction (``commit=False``)."""
    ip = None
    ua = None
    if request is not None:
        fwd = request.headers.get("x-forwarded-for")
        ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None))
        ua = (request.headers.get("user-agent") or "")[:300]
    db.add(
        AdminAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            summary=(summary or "")[:300] or None,
            before_json=_jsonable(before),
            after_json=_jsonable(after),
            ip=ip,
            user_agent=ua,
            created_at=datetime.utcnow(),
        )
    )
    if commit:
        db.commit()


def roles_with(permission: str) -> Iterable[str]:
    return [r for r, p in PERMISSIONS.items() if "*" in p or permission in p]
