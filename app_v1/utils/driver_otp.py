"""Purpose-bound driver OTP helpers (PR14).

Reuses hash/SMS primitives from PR5 otp utils but never issues reset_token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.driver_otp import DriverOtpChallenge, DriverOtpToken
from ..utils.common import ErrorResponse
from ..utils.otp import hash_otp, send_exotel_sms

load_dotenv()

DRIVER_OTP_EXPIRY_MINUTES = int(os.getenv("DRIVER_OTP_EXPIRY_MINUTES", "5"))
DRIVER_OTP_MAX_ATTEMPTS = int(os.getenv("DRIVER_OTP_MAX_ATTEMPTS", "5"))
DRIVER_OTP_TOKEN_EXPIRY_MINUTES = int(
    os.getenv("DRIVER_OTP_TOKEN_EXPIRY_MINUTES", "10")
)

PURPOSE_CREATE_DRIVER = "CREATE_DRIVER"
PURPOSE_CHANGE_DRIVER_PHONE = "CHANGE_DRIVER_PHONE"
VALID_PURPOSES = {PURPOSE_CREATE_DRIVER, PURPOSE_CHANGE_DRIVER_PHONE}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _otp_pepper() -> str:
    return os.getenv("OTP_PEPPER") or os.getenv("JWT_SECRET") or "openbid-otp-pepper"


def hash_driver_otp_token(token: str) -> str:
    material = f"{_otp_pepper()}:driver_otp_token:{token}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def normalize_driver_phone(value: Optional[str]) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits


def validate_driver_phone(phone: str) -> Optional[str]:
    """Return normalized digits or None when invalid."""
    digits = normalize_driver_phone(phone)
    if len(digits) < 10:
        return None
    return digits


def _scope_driver_id(driver_id: Optional[int]) -> int:
    if driver_id is None:
        return 0
    try:
        return int(driver_id)
    except (TypeError, ValueError):
        return 0


def upsert_driver_otp_challenge(
    db: Session,
    *,
    vendor_app_id: str,
    driver_phone: str,
    purpose: str,
    otp: str,
    driver_id: Optional[int] = None,
) -> None:
    """Store hashed OTP; resend replaces previous challenge and resets attempts."""
    now = _utcnow()
    expires_at = now + timedelta(minutes=DRIVER_OTP_EXPIRY_MINUTES)
    otp_digest = hash_otp(otp)
    scoped_driver_id = _scope_driver_id(driver_id)

    existing = (
        db.query(DriverOtpChallenge)
        .filter(
            DriverOtpChallenge.vendor_app_id == vendor_app_id,
            DriverOtpChallenge.driver_phone == driver_phone,
            DriverOtpChallenge.purpose == purpose,
            DriverOtpChallenge.driver_id == scoped_driver_id,
        )
        .first()
    )
    if existing:
        existing.otp_hash = otp_digest
        existing.expires_at = expires_at
        existing.attempt_count = 0
        existing.updated_at = now
    else:
        db.add(
            DriverOtpChallenge(
                vendor_app_id=vendor_app_id,
                driver_phone=driver_phone,
                purpose=purpose,
                driver_id=scoped_driver_id,
                otp_hash=otp_digest,
                expires_at=expires_at,
                attempt_count=0,
            )
        )
    db.commit()


def _issue_driver_otp_token(
    db: Session,
    *,
    vendor_app_id: str,
    driver_phone: str,
    purpose: str,
    driver_id: Optional[int] = None,
) -> str:
    raw_token = secrets.token_urlsafe(32)
    token_digest = hash_driver_otp_token(raw_token)
    now = _utcnow()
    scoped_driver_id = _scope_driver_id(driver_id)
    db.add(
        DriverOtpToken(
            vendor_app_id=vendor_app_id,
            driver_phone=driver_phone,
            purpose=purpose,
            driver_id=scoped_driver_id,
            token_hash=token_digest,
            expires_at=now + timedelta(minutes=DRIVER_OTP_TOKEN_EXPIRY_MINUTES),
            used=False,
        )
    )
    db.commit()
    return raw_token


def send_driver_otp(
    db: Session,
    *,
    vendor_app_id: str,
    driver_phone: str,
    purpose: str,
    driver_id: Optional[int] = None,
) -> ErrorResponse:
    """Generate OTP, store hash, send SMS. Never returns OTP in response."""
    phone = validate_driver_phone(driver_phone)
    if phone is None:
        return ErrorResponse(message="ERROR_INVALID_PHONE")

    if purpose not in VALID_PURPOSES:
        return ErrorResponse(message="ERROR_INVALID_PURPOSE")

    bypass_sms = os.getenv("OTP_TEST_BYPASS_SMS", "").strip() == "1"
    fixed = os.getenv("OTP_TEST_FIXED_OTP", "").strip()
    if bypass_sms and fixed:
        otp = fixed
    else:
        otp = "".join(random.choices(string.digits, k=4))

    try:
        upsert_driver_otp_challenge(
            db,
            vendor_app_id=vendor_app_id,
            driver_phone=phone,
            purpose=purpose,
            otp=otp,
            driver_id=driver_id,
        )
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")

    if bypass_sms:
        return ErrorResponse(message="OTP_SENT")

    e164 = phone if phone.startswith("+") else f"+91{phone[-10:]}"
    sms_result = send_exotel_sms(
        e164,
        f"Your OpenBid driver verification code is {otp}. Valid for "
        f"{DRIVER_OTP_EXPIRY_MINUTES} minutes.",
    )
    if sms_result.get("message") != "SMS_SENT":
        return ErrorResponse(message="ERROR_SENDING_SMS")

    return ErrorResponse(message="OTP_SENT")


def verify_driver_otp(
    db: Session,
    *,
    vendor_app_id: str,
    driver_phone: str,
    purpose: str,
    otp: str,
    driver_id: Optional[int] = None,
) -> Union[dict, ErrorResponse]:
    """
    Verify OTP challenge and issue a short-lived driverOtpToken.

    Success dict: {message: OTP_VERIFIED, driverOtpToken: ...}
    """
    phone = validate_driver_phone(driver_phone)
    if phone is None:
        return ErrorResponse(message="ERROR_INVALID_PHONE")

    if purpose not in VALID_PURPOSES:
        return ErrorResponse(message="ERROR_INVALID_PURPOSE")

    otp_clean = str(otp or "").strip()
    if not otp_clean or not otp_clean.isdigit():
        return ErrorResponse(message="ERROR_INVALID_OTP")

    scoped_driver_id = _scope_driver_id(driver_id)
    now = _utcnow()

    try:
        challenge = (
            db.query(DriverOtpChallenge)
            .filter(
                DriverOtpChallenge.vendor_app_id == vendor_app_id,
                DriverOtpChallenge.driver_phone == phone,
                DriverOtpChallenge.purpose == purpose,
                DriverOtpChallenge.driver_id == scoped_driver_id,
            )
            .first()
        )
        if challenge is None:
            return ErrorResponse(message="ERROR_INVALID_OTP")

        if challenge.expires_at is not None and challenge.expires_at < now:
            db.delete(challenge)
            db.commit()
            return ErrorResponse(message="ERROR_OTP_EXPIRED")

        if (challenge.attempt_count or 0) >= DRIVER_OTP_MAX_ATTEMPTS:
            return ErrorResponse(message="ERROR_TOO_MANY_ATTEMPTS")

        expected = challenge.otp_hash or ""
        actual = hash_otp(otp_clean)
        if not _constant_time_equals(expected, actual):
            challenge.attempt_count = (challenge.attempt_count or 0) + 1
            db.commit()
            if challenge.attempt_count >= DRIVER_OTP_MAX_ATTEMPTS:
                return ErrorResponse(message="ERROR_TOO_MANY_ATTEMPTS")
            return ErrorResponse(message="ERROR_INVALID_OTP")

        # Success: remove challenge then issue mutation token
        db.delete(challenge)
        db.commit()

        token = _issue_driver_otp_token(
            db,
            vendor_app_id=vendor_app_id,
            driver_phone=phone,
            purpose=purpose,
            driver_id=driver_id,
        )
        return {"message": "OTP_VERIFIED", "driverOtpToken": token}

    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")


def validate_driver_otp_token(
    db: Session,
    *,
    raw_token: str,
    vendor_app_id: str,
    driver_phone: str,
    purpose: str,
    driver_id: Optional[int] = None,
) -> Union[DriverOtpToken, ErrorResponse]:
    """Validate an unused, unexpired token matching the mutation scope. Does not consume."""
    token_clean = str(raw_token or "").strip()
    if not token_clean:
        return ErrorResponse(message="ERROR_OTP_TOKEN_REQUIRED")

    phone = validate_driver_phone(driver_phone)
    if phone is None:
        return ErrorResponse(message="ERROR_INVALID_PHONE")

    if purpose not in VALID_PURPOSES:
        return ErrorResponse(message="ERROR_INVALID_PURPOSE")

    scoped_driver_id = _scope_driver_id(driver_id)
    now = _utcnow()
    digest = hash_driver_otp_token(token_clean)

    row = (
        db.query(DriverOtpToken)
        .filter(DriverOtpToken.token_hash == digest)
        .first()
    )
    if row is None:
        return ErrorResponse(message="ERROR_INVALID_OTP_TOKEN")

    if row.used:
        return ErrorResponse(message="ERROR_INVALID_OTP_TOKEN")

    if row.expires_at is not None and row.expires_at < now:
        return ErrorResponse(message="ERROR_OTP_TOKEN_EXPIRED")

    if str(row.vendor_app_id) != str(vendor_app_id):
        return ErrorResponse(message="ERROR_INVALID_OTP_TOKEN")

    if str(row.driver_phone) != phone:
        return ErrorResponse(message="ERROR_INVALID_OTP_TOKEN")

    if str(row.purpose) != purpose:
        return ErrorResponse(message="ERROR_INVALID_OTP_TOKEN")

    if int(row.driver_id or 0) != scoped_driver_id:
        return ErrorResponse(message="ERROR_INVALID_OTP_TOKEN")

    return row


def consume_driver_otp_token(db: Session, token_row: DriverOtpToken) -> None:
    """Mark token used. Caller must commit with the mutation transaction."""
    token_row.used = True
    token_row.used_at = _utcnow()
