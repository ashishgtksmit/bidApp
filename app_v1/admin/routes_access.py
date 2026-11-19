"""Admin sign-in, own account, team management and audit log."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from .common import PageParams, iso
from .models import ROLES, AdminAuditLog, AdminUser
from .security import (
    LOCKOUT_MINUTES,
    MAX_FAILED_LOGINS,
    PERMISSIONS,
    CurrentAdmin,
    audit,
    get_current_admin,
    hash_password,
    issue_token,
    require,
    require_reader,
    validate_new_password,
    verify_password,
)

router = APIRouter()


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=200)
    newPassword: str = Field(min_length=12, max_length=200)


class TeamCreateIn(BaseModel):
    email: EmailStr
    fullName: str = Field(min_length=2, max_length=200)
    role: str = Field(pattern="^(" + "|".join(ROLES) + ")$")


class TeamUpdateIn(BaseModel):
    role: Optional[str] = Field(default=None, pattern="^(" + "|".join(ROLES) + ")$")
    isActive: Optional[bool] = None
    fullName: Optional[str] = Field(default=None, min_length=2, max_length=200)


def _admin_out(a: AdminUser) -> dict:
    return {
        "id": a.id,
        "email": a.email,
        "fullName": a.full_name,
        "role": a.role,
        "isActive": bool(a.is_active),
        "mustChangePassword": bool(a.must_change_password),
        "lastLoginAt": iso(a.last_login_at),
        "createdAt": iso(a.created_at),
    }


def _me_out(a: AdminUser) -> dict:
    out = _admin_out(a)
    perms = PERMISSIONS.get(a.role, set())
    out["permissions"] = sorted(perms)
    return out


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    email = body.email.strip().lower()
    admin = db.query(AdminUser).filter(AdminUser.email == email).first()
    now = datetime.utcnow()
    generic = HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
    if admin is None:
        verify_password(body.password, hash_password("timing-equaliser"))
        raise generic
    if admin.locked_until and admin.locked_until > now:
        raise HTTPException(status_code=429, detail="ACCOUNT_TEMPORARILY_LOCKED")
    if not verify_password(body.password, admin.password_hash) or not admin.is_active:
        admin.failed_logins = int(admin.failed_logins or 0) + 1
        if admin.failed_logins >= MAX_FAILED_LOGINS:
            admin.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            admin.failed_logins = 0
        db.commit()
        raise generic
    admin.failed_logins = 0
    admin.locked_until = None
    admin.last_login_at = now
    token, exp = issue_token(admin)
    me = CurrentAdmin(admin.id, admin.email, admin.full_name, admin.role, bool(admin.must_change_password))
    audit(db, me, request, action="auth.login", entity_type="admin", entity_id=admin.id)
    db.commit()
    return {"accessToken": token, "expiresAt": exp.isoformat(), "admin": _me_out(admin)}


@router.get("/auth/me")
def me(current: CurrentAdmin = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict:
    admin = db.query(AdminUser).filter(AdminUser.id == current.id).one()
    return _me_out(admin)


@router.post("/auth/change-password")
def change_password(
    body: ChangePasswordIn,
    request: Request,
    current: CurrentAdmin = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    admin = db.query(AdminUser).filter(AdminUser.id == current.id).one()
    if not verify_password(body.currentPassword, admin.password_hash):
        raise HTTPException(status_code=400, detail="CURRENT_PASSWORD_WRONG")
    validate_new_password(body.newPassword)
    if body.newPassword == body.currentPassword:
        raise HTTPException(status_code=422, detail="PASSWORD_UNCHANGED")
    admin.password_hash = hash_password(body.newPassword)
    admin.must_change_password = False
    admin.token_version = int(admin.token_version) + 1
    audit(db, current, request, action="auth.change_password", entity_type="admin", entity_id=admin.id)
    db.commit()
    token, exp = issue_token(admin)
    return {"accessToken": token, "expiresAt": exp.isoformat(), "admin": _me_out(admin)}


@router.get("/team")
def list_team(_: CurrentAdmin = Depends(require("team.manage")), db: Session = Depends(get_db)) -> list[dict]:
    return [_admin_out(a) for a in db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()]


@router.post("/team")
def create_member(
    body: TeamCreateIn,
    request: Request,
    current: CurrentAdmin = Depends(require("team.manage")),
    db: Session = Depends(get_db),
) -> dict:
    email = body.email.strip().lower()
    if db.query(AdminUser).filter(AdminUser.email == email).first():
        raise HTTPException(status_code=409, detail="EMAIL_ALREADY_EXISTS")
    temp = _temp_password()
    a = AdminUser(
        email=email,
        full_name=body.fullName.strip(),
        role=body.role,
        password_hash=hash_password(temp),
        is_active=True,
        must_change_password=True,
        token_version=1,
        failed_logins=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(a)
    db.flush()
    audit(db, current, request, action="team.create", entity_type="admin", entity_id=a.id,
          summary=f"Added {email} as {body.role}", after={"email": email, "role": body.role})
    db.commit()
    # The temporary password is shown once to the inviting admin; never stored in clear.
    return {"member": _admin_out(a), "temporaryPassword": temp}


@router.patch("/team/{admin_id}")
def update_member(
    admin_id: int,
    body: TeamUpdateIn,
    request: Request,
    current: CurrentAdmin = Depends(require("team.manage")),
    db: Session = Depends(get_db),
) -> dict:
    a = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if a is None:
        raise HTTPException(status_code=404, detail="ADMIN_NOT_FOUND")
    if a.id == current.id and (body.isActive is False or (body.role and body.role != "super_admin")):
        raise HTTPException(status_code=409, detail="CANNOT_DEMOTE_OR_DEACTIVATE_SELF")
    before = _admin_out(a)
    if body.role is not None:
        a.role = body.role
    if body.fullName is not None:
        a.full_name = body.fullName.strip()
    if body.isActive is not None:
        a.is_active = body.isActive
    if body.role is not None or body.isActive is False:
        a.token_version = int(a.token_version) + 1
    audit(db, current, request, action="team.update", entity_type="admin", entity_id=a.id,
          summary=f"Updated {a.email}", before=before, after=_admin_out(a))
    db.commit()
    return _admin_out(a)


@router.post("/team/{admin_id}/reset-password")
def reset_member_password(
    admin_id: int,
    request: Request,
    current: CurrentAdmin = Depends(require("team.manage")),
    db: Session = Depends(get_db),
) -> dict:
    a = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if a is None:
        raise HTTPException(status_code=404, detail="ADMIN_NOT_FOUND")
    temp = _temp_password()
    a.password_hash = hash_password(temp)
    a.must_change_password = True
    a.token_version = int(a.token_version) + 1
    a.failed_logins = 0
    a.locked_until = None
    audit(db, current, request, action="team.reset_password", entity_type="admin", entity_id=a.id,
          summary=f"Reset password for {a.email}")
    db.commit()
    return {"temporaryPassword": temp}


@router.get("/audit")
def list_audit(
    q: Optional[str] = Query(None, max_length=100),
    entityType: Optional[str] = Query(None, max_length=40),
    entityId: Optional[str] = Query(None, max_length=64),
    pg: PageParams = Depends(),
    _: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(AdminAuditLog)
    if entityType:
        query = query.filter(AdminAuditLog.entity_type == entityType)
    if entityId:
        query = query.filter(AdminAuditLog.entity_id == entityId)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(AdminAuditLog.admin_email.like(like), AdminAuditLog.action.like(like),
                                 AdminAuditLog.summary.like(like)))
    rows, total = pg.apply(query.order_by(AdminAuditLog.id.desc()))
    return pg.wrap([
        {
            "id": r.id,
            "adminEmail": r.admin_email,
            "action": r.action,
            "entityType": r.entity_type,
            "entityId": r.entity_id,
            "summary": r.summary,
            "before": r.before_json,
            "after": r.after_json,
            "ip": r.ip,
            "createdAt": iso(r.created_at),
        }
        for r in rows
    ], total)


def _temp_password() -> str:
    # 16 chars, guaranteed upper/lower/digit to pass validate_new_password on first change.
    return "Ob" + secrets.token_urlsafe(10) + "7k"
