"""Admin identity + audit tables (created by migrations/admin_console/001_admin_tables.sql)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)

from ..database import Base

_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")

ROLES = ("super_admin", "operations", "finance", "support", "viewer")


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(200), nullable=False, unique=True)
    full_name = Column(String(200), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="viewer")
    is_active = Column(Boolean, nullable=False, default=True)
    # Bumped on password change / deactivate so old tokens stop working.
    token_version = Column(Integer, nullable=False, default=1)
    must_change_password = Column(Boolean, nullable=False, default=True)
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("ix_admin_audit_entity", "entity_type", "entity_id"),
        Index("ix_admin_audit_created", "created_at"),
    )

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    admin_id = Column(Integer, nullable=True)
    admin_email = Column(String(200), nullable=False)
    action = Column(String(60), nullable=False)
    entity_type = Column(String(40), nullable=False)
    entity_id = Column(String(64), nullable=True)
    summary = Column(String(300), nullable=True)
    before_json = Column(Text, nullable=True)
    after_json = Column(Text, nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(300), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
