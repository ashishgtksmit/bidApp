"""Dedicated driver-phone OTP challenges and mutation tokens (PR14).

Separate from PR5 password-reset OTP / reset_token tables.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)

from ..database import Base


class DriverOtpChallenge(Base):
    """Hashed OTP challenge scoped to vendor + phone + purpose (+ driver)."""

    __tablename__ = "driver_otp_challenges"
    __table_args__ = (
        UniqueConstraint(
            "vendor_app_id",
            "driver_phone",
            "purpose",
            "driver_id",
            name="uq_driver_otp_challenge_scope",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_app_id = Column(String(20), nullable=False, index=True)
    driver_phone = Column(String(20), nullable=False, index=True)
    purpose = Column(String(40), nullable=False, index=True)
    # 0 = create / no driver; otherwise DRIVERID for CHANGE_DRIVER_PHONE
    driver_id = Column(BigInteger, nullable=False, default=0)
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


class DriverOtpToken(Base):
    """One-time mutation authorization token after successful driver OTP verify."""

    __tablename__ = "driver_otp_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_app_id = Column(String(20), nullable=False, index=True)
    driver_phone = Column(String(20), nullable=False, index=True)
    purpose = Column(String(40), nullable=False, index=True)
    driver_id = Column(BigInteger, nullable=False, default=0)
    token_hash = Column(String(128), nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
