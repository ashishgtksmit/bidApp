"""PR49 vendor bank-account security: OTP on bank change and penny-drop
verification before withdrawal. Both are off unless enabled in App Settings:

  BANK_CHANGE_OTP_REQUIRED=1   PUT /updatevendorbankdetails needs an ``otp``
                               whenever the details change.
  BANK_VERIFICATION_ENABLED=1  Withdrawals need a Cashfree-verified account.

Verification policy: an invalid account OR a name mismatch blocks withdrawal.
A name result counts as a match when it is in
``BANK_VERIFICATION_ACCEPTED_NAME_MATCHES`` (default DIRECT_MATCH,
GOOD_PARTIAL_MATCH, MODERATE_PARTIAL_MATCH).
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..models.user_table import User
from ..models.vendor_wallet import VendorBankChangeOtpChallenge, VendorBankVerification
from ..services import cashfree_verification
from ..services.cashfree_pg import CashfreeError, CashfreeNotConfigured
from ..services.vendor_otp import consume_otp, issue_otp
from ..utils.otp import _otp_pepper
from ..utils.rate_limit import enforce_rate_limit

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
BANK_CHANGE_OTP_PURPOSE = "bank-change"

VERIFIED = "VERIFIED"
INVALID = "INVALID"
NAME_MISMATCH = "NAME_MISMATCH"
NOT_VERIFIED = "NOT_VERIFIED"

DEFAULT_ACCEPTED_NAME_MATCHES = "DIRECT_MATCH,GOOD_PARTIAL_MATCH,MODERATE_PARTIAL_MATCH"


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


# --- OTP on bank change -----------------------------------------------------


def bank_change_otp_required() -> bool:
    return (os.getenv("BANK_CHANGE_OTP_REQUIRED") or "").strip() == "1"


def send_bank_change_otp(db: Session, current_user: AuthenticatedUser) -> int:
    """SMS an OTP to the vendor's registered mobile. Returns expiry seconds."""
    from .user import _enforce_bank_vendor_eligibility

    try:
        user = db.query(User).filter(User.UID == current_user.uid).first()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="BANK_OTP_FAILED") from None
    if user is None:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
    _enforce_bank_vendor_eligibility(user)
    return issue_otp(
        db,
        model=VendorBankChangeOtpChallenge,
        purpose=BANK_CHANGE_OTP_PURPOSE,
        user_uid=int(user.UID),
        phone=str(user.userAppId or ""),
        rate_bucket="bank_change_otp",
    )


def require_bank_change_otp(db: Session, user: User, otp: Optional[str]) -> None:
    """Called inside the bank-update transaction when the details change.

    No-op unless ``BANK_CHANGE_OTP_REQUIRED=1``. On success the challenge
    delete joins the caller's commit.
    """
    if not bank_change_otp_required():
        return
    if not otp:
        raise HTTPException(status_code=422, detail="BANK_CHANGE_OTP_REQUIRED")
    consume_otp(
        db,
        model=VendorBankChangeOtpChallenge,
        purpose=BANK_CHANGE_OTP_PURPOSE,
        user_uid=int(user.UID),
        otp=otp,
        commit_on_success=False,
    )


# --- Penny drop ---------------------------------------------------------------


def _accepted_name_matches() -> set:
    raw = os.getenv("BANK_VERIFICATION_ACCEPTED_NAME_MATCHES") or DEFAULT_ACCEPTED_NAME_MATCHES
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _details_hash(user: User) -> str:
    material = ":".join(
        [
            _otp_pepper(),
            "bank-verification",
            str(int(user.UID)),
            str(user.bankAccountNo or "").strip(),
            str(user.bankIFSC or "").strip().upper(),
            cashfree_verification.clean_holder_name(user.bankAccountHolderName or "").upper(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cached(db: Session, user: User) -> Optional[VendorBankVerification]:
    return (
        db.query(VendorBankVerification)
        .filter(
            VendorBankVerification.user_uid == int(user.UID),
            VendorBankVerification.details_hash == _details_hash(user),
        )
        .first()
    )


def bank_verification_status(db: Session, user: User) -> Optional[str]:
    """For preflight. None when verification is disabled. Never calls Cashfree."""
    if not cashfree_verification.verification_enabled():
        return None
    row = _cached(db, user)
    return row.status if row is not None else NOT_VERIFIED


def verification_note(row: Optional[VendorBankVerification]) -> str:
    if row is None:
        return "Not checked (bank verification disabled)"
    parts = [row.status]
    if row.name_match_result:
        parts.append(f"name match {row.name_match_result}")
    if row.name_at_bank:
        parts.append(f"name at bank: {row.name_at_bank}")
    if row.reference_id:
        parts.append(f"Cashfree ref {row.reference_id}")
    return "; ".join(parts)[:255]


def _raise_for(row: VendorBankVerification) -> None:
    if row.status == INVALID:
        raise HTTPException(status_code=422, detail="BANK_ACCOUNT_INVALID")
    if row.status == NAME_MISMATCH:
        raise HTTPException(status_code=422, detail="BANK_NAME_MISMATCH")


def ensure_bank_verified(db: Session, user: User) -> Optional[VendorBankVerification]:
    """Block withdrawal unless the current bank details are verified.

    Returns the verification row, or None when verification is disabled.
    Raises 422 ``BANK_ACCOUNT_INVALID`` / ``BANK_NAME_MISMATCH``, 429
    ``RATE_LIMITED``, 503 ``BANK_VERIFICATION_UNAVAILABLE``. Must not be
    called while holding row locks (it makes an HTTP call).
    """
    if not cashfree_verification.verification_enabled():
        return None
    try:
        row = _cached(db, user)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="WALLET_QUERY_FAILED") from None
    if row is not None:
        _raise_for(row)
        return row

    try:
        config = cashfree_verification.load_verification_config()
    except CashfreeNotConfigured:
        logger.error("BANK_VERIFICATION_NOT_CONFIGURED")
        raise HTTPException(status_code=503, detail="BANK_VERIFICATION_UNAVAILABLE") from None

    limited = enforce_rate_limit(
        db,
        bucket_key=f"bank_verification:uid:{int(user.UID)}",
        max_hits=int(os.getenv("RATE_LIMIT_BANK_VERIFICATION_PER_USER", "3")),
        window_seconds=int(os.getenv("RATE_LIMIT_BANK_VERIFICATION_WINDOW_SECONDS", "86400")),
        fail_closed=True,
    )
    if limited is not None:
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    try:
        result = cashfree_verification.verify_bank_account(
            config,
            bank_account=str(user.bankAccountNo or ""),
            ifsc=str(user.bankIFSC or ""),
            name=str(user.bankAccountHolderName or ""),
            phone=str(user.userAppId or ""),
        )
    except CashfreeError:
        raise HTTPException(status_code=503, detail="BANK_VERIFICATION_UNAVAILABLE") from None

    if not result.account_valid:
        status = INVALID
    elif (result.name_match_result or "").upper() in _accepted_name_matches():
        status = VERIFIED
    else:
        status = NAME_MISMATCH

    row = VendorBankVerification(
        user_uid=int(user.UID),
        details_hash=_details_hash(user),
        status=status,
        account_status_code=result.account_status_code,
        name_match_result=result.name_match_result,
        name_at_bank=result.name_at_bank,
        reference_id=result.reference_id,
        environment=config.environment,
        created_at=_now(),
    )
    try:
        db.add(row)
        db.commit()
    except IntegrityError:
        db.rollback()
        row = _cached(db, user)
        if row is None:
            raise HTTPException(status_code=500, detail="WALLET_QUERY_FAILED") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="WALLET_QUERY_FAILED") from None
    logger.info("BANK_VERIFICATION_RESULT uid=%s status=%s", int(user.UID), row.status)
    _raise_for(row)
    return row
