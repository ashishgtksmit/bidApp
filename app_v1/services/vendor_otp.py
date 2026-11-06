"""Hashed SMS OTP challenges for vendor money actions (withdrawal, bank change).

One active challenge per user per purpose table. OTPs are 6 digits from
``secrets``, stored only as a peppered SHA-256, expire after 5 minutes and
lock after 5 wrong attempts. ``OTP_TEST_BYPASS_SMS=1`` skips Exotel and uses
``OTP_TEST_FIXED_OTP`` (non-production only).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Type
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..utils.otp import _otp_pepper, send_exotel_sms, wizzride_otp_sms_body
from ..utils.rate_limit import enforce_rate_limit

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
OTP_EXPIRY_SECONDS = 300
OTP_MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _hash(purpose: str, user_uid: int, otp: str) -> str:
    material = f"{_otp_pepper()}:{purpose}:{user_uid}:{otp}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def issue_otp(
    db: Session,
    *,
    model: Type,
    purpose: str,
    user_uid: int,
    phone: str,
    rate_bucket: str,
) -> int:
    """Rate-limit, send, and store a new OTP. Returns expiry seconds.

    Raises 429 ``RATE_LIMITED``, 503 ``OTP_SEND_FAILED``, 500 on DB error.
    """
    limited = enforce_rate_limit(
        db,
        bucket_key=f"{rate_bucket}:uid:{user_uid}",
        max_hits=int(os.getenv("RATE_LIMIT_WALLET_OTP_PER_USER", "5")),
        window_seconds=int(os.getenv("RATE_LIMIT_WALLET_OTP_WINDOW_SECONDS", "900")),
        fail_closed=True,
    )
    if limited is not None:
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    otp = f"{secrets.randbelow(1_000_000):06d}"
    if (os.getenv("OTP_TEST_BYPASS_SMS") or "").strip() == "1":
        otp = (os.getenv("OTP_TEST_FIXED_OTP") or "").strip() or otp
    else:
        phone = str(phone or "").strip()
        to = phone if phone.startswith("+") else f"+91{phone}"
        result = send_exotel_sms(to, wizzride_otp_sms_body(otp, OTP_EXPIRY_SECONDS // 60))
        if result.get("message") != "SMS_SENT":
            logger.error("VENDOR_OTP_SMS_FAILED purpose=%s code=%s", purpose, result.get("message"))
            raise HTTPException(status_code=503, detail="OTP_SEND_FAILED")

    try:
        challenge = (
            db.query(model).filter(model.user_uid == user_uid).with_for_update().first()
        )
        expires_at = _now() + timedelta(seconds=OTP_EXPIRY_SECONDS)
        if challenge is None:
            db.add(
                model(
                    user_uid=user_uid,
                    otp_hash=_hash(purpose, user_uid, otp),
                    expires_at=expires_at,
                    attempt_count=0,
                )
            )
        else:
            challenge.otp_hash = _hash(purpose, user_uid, otp)
            challenge.expires_at = expires_at
            challenge.attempt_count = 0
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="OTP_STORE_FAILED") from None
    return OTP_EXPIRY_SECONDS


def consume_otp(
    db: Session,
    *,
    model: Type,
    purpose: str,
    user_uid: int,
    otp: str,
    commit_on_success: bool = True,
) -> None:
    """Verify and delete the challenge. Raises 422 ``OTP_EXPIRED`` /
    ``OTP_INVALID`` / ``OTP_LOCKED``.

    Failures always commit (attempt counts must persist). With
    ``commit_on_success=False`` the delete joins the caller's transaction, so
    nothing else pending in the session should exist when this can fail.
    """
    challenge = db.query(model).filter(model.user_uid == user_uid).with_for_update().first()
    if challenge is None or challenge.expires_at <= _now():
        if challenge is not None:
            db.delete(challenge)
        db.commit()
        raise HTTPException(status_code=422, detail="OTP_EXPIRED")
    if (challenge.attempt_count or 0) >= OTP_MAX_ATTEMPTS:
        db.delete(challenge)
        db.commit()
        raise HTTPException(status_code=422, detail="OTP_LOCKED")
    if not hmac.compare_digest(challenge.otp_hash, _hash(purpose, user_uid, otp)):
        challenge.attempt_count = (challenge.attempt_count or 0) + 1
        locked = challenge.attempt_count >= OTP_MAX_ATTEMPTS
        if locked:
            db.delete(challenge)
        db.commit()
        raise HTTPException(status_code=422, detail="OTP_LOCKED" if locked else "OTP_INVALID")
    db.delete(challenge)
    if commit_on_success:
        db.commit()
