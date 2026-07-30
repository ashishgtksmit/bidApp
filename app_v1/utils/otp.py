import hashlib
import hmac
import httpx
import os
import random
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union

from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.otp_challenge import OtpChallenge, PasswordResetToken
from ..schemas.user_table import OtpVerifyResponse
from ..utils.common import ErrorResponse

load_dotenv()

OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "5"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
RESET_TOKEN_EXPIRY_MINUTES = int(os.getenv("RESET_TOKEN_EXPIRY_MINUTES", "10"))


def _utcnow() -> datetime:
    # Naive UTC for MySQL TIMESTAMP + SQLite test compatibility.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _otp_pepper() -> str:
    return os.getenv("OTP_PEPPER") or os.getenv("JWT_SECRET") or "openbid-otp-pepper"


def hash_otp(otp: str) -> str:
    material = f"{_otp_pepper()}:otp:{otp}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def hash_reset_token(token: str) -> str:
    material = f"{_otp_pepper()}:reset:{token}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def send_exotel_sms(to: str, body: str) -> Dict[str, Any]:
    api_key = os.getenv('EXOTEL_API_KEY')
    api_token = os.getenv('EXOTEL_API_TOKEN')
    subdomain = os.getenv('EXOTEL_SUBDOMAIN', 'api.exotel.com')
    sid = os.getenv('EXOTEL_SID')

    if not all([api_key, api_token, sid]):
        return {"message": "ERROR_MISSING_CREDENTIALS"}

    # Basic validations
    if not to or not to.strip():
        return {"message": "ERROR_MISSING_TO"}
    if not re.match(r'^\+\d{10,15}$', to):  # E.164: +<country><number>
        return {"message": "ERROR_INVALID_PHONE"}
    if not body or not body.strip():
        return {"message": "ERROR_MISSING_BODY"}
    if len(body) > 160:
        return {"message": "ERROR_BODY_TOO_LONG"}

    url = f"https://{subdomain}/v1/Accounts/{sid}/Sms/send"
    post_data = {
        "From": "WZRIDE",  # must be approved sender ID for your account
        "To": to,
        "Body": body,
    }

    try:
        with httpx.Client() as client:
            resp = client.post(
                url,
                data=post_data,
                auth=(api_key, api_token),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=30,
            )

        if 200 <= resp.status_code < 300:
            try:
                return {"message": "SMS_SENT", "details": resp.json()}
            except ValueError:
                return {"message": "SMS_SENT", "details": resp.text}

        try:
            err_json = resp.json()
            err_msg = err_json.get("message") or err_json.get("error") or str(err_json)
        except ValueError:
            err_msg = resp.text or "Unknown Error"

        return {
            "message": "ERROR_SENDING_SMS",
            "status": resp.status_code,
            "error": f"HTTP {resp.status_code}: {err_msg}",
        }

    except httpx.HTTPError as e:
        return {"message": "ERROR_SENDING_SMS", "error": str(e)}


def upsert_otp_challenge(db: Session, user_app_id: str, otp: str) -> None:
    """Store hashed OTP; resend replaces previous challenge and resets attempts."""
    now = _utcnow()
    expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)
    otp_digest = hash_otp(otp)

    existing = (
        db.query(OtpChallenge)
        .filter(OtpChallenge.user_app_id == user_app_id)
        .first()
    )
    if existing:
        existing.otp_hash = otp_digest
        existing.expires_at = expires_at
        existing.attempt_count = 0
        existing.updated_at = now
    else:
        db.add(
            OtpChallenge(
                user_app_id=user_app_id,
                otp_hash=otp_digest,
                expires_at=expires_at,
                attempt_count=0,
            )
        )
    db.commit()


def _issue_reset_token(db: Session, user_app_id: str) -> str:
    raw_token = secrets.token_urlsafe(32)
    token_digest = hash_reset_token(raw_token)
    now = _utcnow()
    db.add(
        PasswordResetToken(
            user_app_id=user_app_id,
            token_hash=token_digest,
            expires_at=now + timedelta(minutes=RESET_TOKEN_EXPIRY_MINUTES),
            used=False,
        )
    )
    db.commit()
    return raw_token


def verify_otp_for_user(
    db: Session,
    user_app_id: str,
    otp: str,
) -> Union[OtpVerifyResponse, ErrorResponse]:
    if not user_app_id or not str(user_app_id).strip():
        return ErrorResponse(message="ERROR_MISSING_USERAPPID")
    if not otp or not str(otp).strip():
        return ErrorResponse(message="OTP_INVALID")

    user_app_id = str(user_app_id).strip()
    otp = str(otp).strip()
    now = _utcnow()

    try:
        challenge = (
            db.query(OtpChallenge)
            .filter(OtpChallenge.user_app_id == user_app_id)
            .with_for_update(read=False)
            .first()
        )
        if challenge is None:
            return ErrorResponse(message="OTP_EXPIRED")

        expires_at = challenge.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)

        if expires_at <= now:
            db.delete(challenge)
            db.commit()
            return ErrorResponse(message="OTP_EXPIRED")

        if (challenge.attempt_count or 0) >= OTP_MAX_ATTEMPTS:
            db.delete(challenge)
            db.commit()
            return ErrorResponse(message="OTP_LOCKED")

        challenge.attempt_count = (challenge.attempt_count or 0) + 1
        db.flush()

        if not _constant_time_equals(challenge.otp_hash, hash_otp(otp)):
            db.commit()
            if challenge.attempt_count >= OTP_MAX_ATTEMPTS:
                db.delete(challenge)
                db.commit()
                return ErrorResponse(message="OTP_LOCKED")
            return ErrorResponse(message="OTP_INVALID")

        # Success: invalidate OTP challenge and issue one-time reset token.
        db.delete(challenge)
        db.commit()
        reset_token = _issue_reset_token(db, user_app_id)
        return OtpVerifyResponse(message="OTP_VERIFIED", reset_token=reset_token)
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")


def consume_reset_token(
    db: Session,
    user_app_id: str,
    reset_token: str,
) -> Optional[str]:
    """
    Validate and consume a reset token for user_app_id.

    Returns None on success, otherwise an error message code:
    RESET_TOKEN_REQUIRED | RESET_TOKEN_INVALID | RESET_TOKEN_EXPIRED | RESET_TOKEN_USED
    """
    if not reset_token or not str(reset_token).strip():
        return "RESET_TOKEN_REQUIRED"

    user_app_id = str(user_app_id).strip()
    token_digest = hash_reset_token(str(reset_token).strip())
    now = _utcnow()

    try:
        row = (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.token_hash == token_digest)
            .with_for_update(read=False)
            .first()
        )
        if row is None:
            return "RESET_TOKEN_INVALID"
        if row.user_app_id != user_app_id:
            return "RESET_TOKEN_INVALID"
        if row.used:
            return "RESET_TOKEN_USED"

        expires_at = row.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if expires_at <= now:
            return "RESET_TOKEN_EXPIRED"

        row.used = True
        row.used_at = now
        db.commit()
        return None
    except SQLAlchemyError:
        db.rollback()
        return "RESET_TOKEN_INVALID"


def send_otp_to_user(db: Session, user_app_id: str):
    if not user_app_id or not user_app_id.strip():
        return ErrorResponse(message="ERROR_MISSING_USERAPPID")

    user_app_id = user_app_id.strip()
    # Ensure E.164 format before calling send_exotel_sms
    to = user_app_id if user_app_id.startswith('+') else f"+91{user_app_id}"

    try:
        otp = ''.join(random.choices(string.digits, k=4))
        sms_body = (
            f"{otp} is your OTP for the transaction that you are performing in WIZZRIDE. "
            f"This OTP will be valid for the next {OTP_EXPIRY_MINUTES} mins. "
            "Do not share OTP for Security Reason."
        )

        # Test-only hook: skip Exotel SMS. Optional OTP_TEST_FIXED_OTP sets the
        # challenge value for automated tests. Never returned in HTTP responses.
        bypass_sms = os.getenv("OTP_TEST_BYPASS_SMS", "").strip() == "1"
        if bypass_sms:
            fixed = os.getenv("OTP_TEST_FIXED_OTP", "").strip()
            if fixed:
                otp = fixed
            upsert_otp_challenge(db, user_app_id, otp)
            return ErrorResponse(message="OTP_SENT")

        sms_result = send_exotel_sms(to, sms_body)
        if sms_result.get("message") != "SMS_SENT":
            details = sms_result.get("error") or sms_result.get("message") or "Unknown error"
            return ErrorResponse(message=f"ERROR_SENDING_OTP: {details}")

        upsert_otp_challenge(db, user_app_id, otp)
        return ErrorResponse(message="OTP_SENT")

    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
