from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from ..database import Base


class OtpChallenge(Base):
    """Persisted OTP challenge for public pre-login verification (PR5)."""

    __tablename__ = "otp_challenges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_app_id = Column(String(20), nullable=False, unique=True, index=True)
    otp_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PasswordResetToken(Base):
    """One-time password-reset authorization token issued after OTP verify."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_app_id = Column(String(20), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ApiRateLimitBucket(Base):
    """Shared persistent rate-limit counters for multi-instance Azure."""

    __tablename__ = "api_rate_limit_buckets"
    __table_args__ = (
        UniqueConstraint("bucket_key", "window_start", name="uq_rate_limit_bucket_window"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    bucket_key = Column(String(255), nullable=False, index=True)
    window_start = Column(DateTime, nullable=False)
    hit_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
