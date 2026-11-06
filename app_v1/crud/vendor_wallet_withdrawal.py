"""PR48 vendor wallet withdrawal to the registered bank account (manual payout).

Request: OTP-confirmed, holds amount + fee, emails the ops mailbox.
Ops then marks it PAID (with UTR; hold is debited) or REJECTED (hold released)
through internal endpoints. The vendor API never returns the full account no.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..crud.user import _has_bank_account, _mask_bank_account_mobile
from ..crud.vendor_wallet import _load_eligible_vendor
from ..models.user_table import User
from ..models.vendor_wallet import (
    VendorWallet,
    VendorWalletOtpChallenge,
    VendorWalletWithdrawal,
)
from ..schemas.vendor_wallet import (
    OpsWithdrawalItem,
    OpsWithdrawalList,
    WalletBankAccountSummary,
    WalletFeeSlab,
    WalletWithdrawalCreateRequest,
    WalletWithdrawalItem,
    WalletWithdrawalList,
    WalletWithdrawalOtpResponse,
    WalletWithdrawalPreflight,
)
from ..services.vendor_wallet_ledger import (
    DIRECTION_DEBIT,
    InsufficientWalletBalance,
    TXN_TYPE_WITHDRAWAL,
    TXN_TYPE_WITHDRAWAL_FEE,
    apply_wallet_entry,
    hold_funds,
    release_held_funds,
)
from ..crud.vendor_bank_security import (
    bank_verification_status,
    ensure_bank_verified,
    verification_note,
)
from ..services import wallet_notifications
from ..services.vendor_otp import consume_otp, issue_otp
from ..utils.email import send_email

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

STATUS_REQUESTED = "REQUESTED"
STATUS_PAID = "PAID"
STATUS_REJECTED = "REJECTED"
ACTIVE_STATUSES = (STATUS_REQUESTED, STATUS_PAID)

# Same slabs as the WizzRide wallet: <= ₹1,000 / <= ₹25,000 / above.
FEE_SLABS = ((100_000, 708), (2_500_000, 944), (None, 1_770))

WITHDRAWAL_OTP_PURPOSE = "wallet-withdrawal"


def withdrawal_min_paise() -> int:
    return int(os.getenv("WALLET_WITHDRAWAL_MIN_PAISE", "10000"))


def withdrawal_max_paise() -> int:
    return int(os.getenv("WALLET_WITHDRAWAL_MAX_PAISE", "2500000"))


def withdrawal_daily_limit_paise() -> int:
    return int(os.getenv("WALLET_WITHDRAWAL_DAILY_LIMIT_PAISE", "5000000"))


def withdrawal_fee_paise(amount_paise: int) -> int:
    for upper, fee in FEE_SLABS:
        if upper is None or amount_paise <= upper:
            return fee
    return FEE_SLABS[-1][1]


def _ist_now_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else None


def _rupees(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _require_enabled() -> None:
    if (os.getenv("WALLET_WITHDRAWAL_ENABLED") or "").strip() != "1":
        raise HTTPException(status_code=503, detail="WALLET_WITHDRAWAL_DISABLED")


def _db_error(op: str) -> HTTPException:
    logger.error("WALLET_WITHDRAWAL_DB_FAILED op=%s", op)
    return HTTPException(status_code=500, detail="WALLET_QUERY_FAILED")


def _bank_summary(user: User) -> WalletBankAccountSummary:
    account_no = getattr(user, "bankAccountNo", None)
    if not _has_bank_account(account_no):
        return WalletBankAccountSummary(hasBankAccount=False)
    return WalletBankAccountSummary(
        hasBankAccount=True,
        maskedAccountNumber=_mask_bank_account_mobile(account_no),
        accountHolderName=getattr(user, "bankAccountHolderName", None),
        bankIFSC=getattr(user, "bankIFSC", None),
        bankName=getattr(user, "bankName", None),
    )


def _require_bank(user: User) -> None:
    if not (
        _has_bank_account(getattr(user, "bankAccountNo", None))
        and str(getattr(user, "bankIFSC", "") or "").strip()
        and str(getattr(user, "bankAccountHolderName", "") or "").strip()
    ):
        raise HTTPException(status_code=409, detail="BANK_DETAILS_REQUIRED")


def _available_paise(db: Session, user_uid: int) -> int:
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == user_uid).first()
    if wallet is None:
        return 0
    return max(int(wallet.balance_paise or 0) - int(wallet.held_paise or 0), 0)


def _withdrawn_today_paise(db: Session, user_uid: int) -> int:
    start = datetime.combine(_ist_now_naive().date(), time.min)
    total = (
        db.query(func.coalesce(func.sum(VendorWalletWithdrawal.amount_paise), 0))
        .filter(
            VendorWalletWithdrawal.user_uid == user_uid,
            VendorWalletWithdrawal.status.in_(ACTIVE_STATUSES),
            VendorWalletWithdrawal.requested_at >= start,
        )
        .scalar()
    )
    return int(total or 0)


def _check_amount(db: Session, user_uid: int, amount: int) -> int:
    """Validate limits and balance; returns the fee."""
    if amount < withdrawal_min_paise():
        raise HTTPException(status_code=422, detail="WITHDRAWAL_BELOW_MIN")
    if amount > withdrawal_max_paise():
        raise HTTPException(status_code=422, detail="WITHDRAWAL_ABOVE_MAX")
    if _withdrawn_today_paise(db, user_uid) + amount > withdrawal_daily_limit_paise():
        raise HTTPException(status_code=422, detail="WITHDRAWAL_DAILY_LIMIT_EXCEEDED")
    fee = withdrawal_fee_paise(amount)
    if amount + fee > _available_paise(db, user_uid):
        raise HTTPException(status_code=422, detail="INSUFFICIENT_BALANCE")
    return fee


def _to_item(row: VendorWalletWithdrawal) -> WalletWithdrawalItem:
    return WalletWithdrawalItem(
        withdrawalId=row.withdrawal_ref,
        status=row.status,
        amountPaise=int(row.amount_paise),
        feePaise=int(row.fee_paise),
        totalPaise=int(row.amount_paise) + int(row.fee_paise),
        maskedAccountNumber=_mask_bank_account_mobile(row.bank_account_no) or "XXXX",
        bankName=row.bank_name,
        requestedAt=_fmt_dt(row.requested_at) or "",
        resolvedAt=_fmt_dt(row.resolved_at),
        utr=row.utr,
        rejectionReason=row.rejection_reason,
    )


# --- Vendor: preflight ------------------------------------------------------


def get_withdrawal_preflight(
    db: Session,
    current_user: AuthenticatedUser,
) -> WalletWithdrawalPreflight:
    _require_enabled()
    try:
        user = _load_eligible_vendor(db, current_user)
        available = _available_paise(db, current_user.uid)
        today = _withdrawn_today_paise(db, current_user.uid)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("preflight") from None
    daily = withdrawal_daily_limit_paise()
    bank = _bank_summary(user)
    if bank.hasBankAccount:
        try:
            bank.verificationStatus = bank_verification_status(db, user)
        except SQLAlchemyError:
            db.rollback()
            raise _db_error("preflight") from None
    return WalletWithdrawalPreflight(
        bankAccount=bank,
        availablePaise=available,
        minPaise=withdrawal_min_paise(),
        maxPaise=withdrawal_max_paise(),
        dailyLimitPaise=daily,
        dailyRemainingPaise=max(daily - today, 0),
        feeSlabs=[WalletFeeSlab(upToPaise=upper, feePaise=fee) for upper, fee in FEE_SLABS],
    )


# --- Vendor: OTP ------------------------------------------------------------


def send_withdrawal_otp(
    db: Session,
    current_user: AuthenticatedUser,
) -> WalletWithdrawalOtpResponse:
    """Bank check, then penny drop (when enabled), then SMS OTP — so a vendor
    with an unverifiable account learns it before receiving an OTP."""
    _require_enabled()
    try:
        user = _load_eligible_vendor(db, current_user)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("otp_user") from None
    _require_bank(user)
    ensure_bank_verified(db, user)

    expires_in = issue_otp(
        db,
        model=VendorWalletOtpChallenge,
        purpose=WITHDRAWAL_OTP_PURPOSE,
        user_uid=current_user.uid,
        phone=str(getattr(user, "userAppId", "") or ""),
        rate_bucket="wallet_withdrawal_otp",
    )
    return WalletWithdrawalOtpResponse(message="OTP_SENT", expiresInSeconds=expires_in)


def _consume_otp(db: Session, user_uid: int, otp: str) -> None:
    consume_otp(
        db,
        model=VendorWalletOtpChallenge,
        purpose=WITHDRAWAL_OTP_PURPOSE,
        user_uid=user_uid,
        otp=otp,
    )


# --- Vendor: create / list --------------------------------------------------


def _find_by_client_key(db: Session, user_uid: int, key: str) -> Optional[VendorWalletWithdrawal]:
    return (
        db.query(VendorWalletWithdrawal)
        .filter(
            VendorWalletWithdrawal.user_uid == user_uid,
            VendorWalletWithdrawal.client_idempotency_key == key,
        )
        .first()
    )


def create_withdrawal(
    db: Session,
    current_user: AuthenticatedUser,
    payload: WalletWithdrawalCreateRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> WalletWithdrawalItem:
    _require_enabled()
    uid = current_user.uid
    amount = payload.amountPaise
    try:
        user = _load_eligible_vendor(db, current_user)
        existing = _find_by_client_key(db, uid, payload.idempotencyKey)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("create_lookup") from None
    if existing is not None:
        if int(existing.amount_paise) != amount:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_REUSED")
        return _to_item(existing)

    _require_bank(user)
    verification = ensure_bank_verified(db, user)
    try:
        _check_amount(db, uid, amount)
        _consume_otp(db, uid, payload.otp)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("create_precheck") from None

    try:
        # Re-check under the wallet row lock; serializes this user's withdrawals.
        db.query(VendorWallet).filter(VendorWallet.user_uid == uid).with_for_update().first()
        fee = _check_amount(db, uid, amount)
        hold_funds(db, user_uid=uid, amount_paise=amount + fee)
        row = VendorWalletWithdrawal(
            withdrawal_ref=f"OBWD{secrets.token_hex(10).upper()}",
            user_uid=uid,
            amount_paise=amount,
            fee_paise=fee,
            status=STATUS_REQUESTED,
            client_idempotency_key=payload.idempotencyKey,
            bank_account_holder_name=str(user.bankAccountHolderName).strip(),
            bank_account_no=str(user.bankAccountNo).strip(),
            bank_ifsc=str(user.bankIFSC).strip().upper(),
            bank_name=(str(user.bankName).strip() if user.bankName else None),
            bank_verification_note=verification_note(verification),
            requested_at=_ist_now_naive(),
        )
        db.add(row)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except InsufficientWalletBalance:
        db.rollback()
        raise HTTPException(status_code=422, detail="INSUFFICIENT_BALANCE") from None
    except IntegrityError:
        db.rollback()
        raced = _find_by_client_key(db, uid, payload.idempotencyKey)
        if raced is None:
            raise _db_error("create_insert") from None
        return _to_item(raced)
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("create_insert") from None

    logger.info("WALLET_WITHDRAWAL_REQUESTED ref=%s", row.withdrawal_ref)
    _notify_ops(db, row, user)
    _push(
        background_tasks,
        uid,
        "Withdrawal requested",
        f"Your withdrawal of {_rupees(amount)} is being processed. The amount is on hold until it is transferred.",
    )
    return _to_item(row)


def _push(background_tasks: Optional[BackgroundTasks], uid: int, title: str, body: str) -> None:
    wallet_notifications.dispatch(
        background_tasks, wallet_notifications.notify_vendor_wallet, int(uid), title, body
    )


def list_withdrawals(
    db: Session,
    current_user: AuthenticatedUser,
) -> WalletWithdrawalList:
    try:
        _load_eligible_vendor(db, current_user)
        rows = (
            db.query(VendorWalletWithdrawal)
            .filter(VendorWalletWithdrawal.user_uid == current_user.uid)
            .order_by(VendorWalletWithdrawal.id.desc())
            .limit(20)
            .all()
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("list") from None
    return WalletWithdrawalList(items=[_to_item(r) for r in rows])


# --- Ops email ---------------------------------------------------------------


def _ops_email_body(row: VendorWalletWithdrawal, user: User) -> str:
    total = int(row.amount_paise) + int(row.fee_paise)
    api_base = (os.getenv("OPENBID_PUBLIC_API_BASE_URL") or "<OpenBid API base URL>").rstrip("/")
    return "\n".join(
        [
            "A vendor has requested a withdrawal from their OpenBid wallet.",
            "",
            f"Withdrawal ID     : {row.withdrawal_ref}",
            f"Requested at (IST): {_fmt_dt(row.requested_at)}",
            "",
            "VENDOR",
            f"Name              : {getattr(user, 'fullName', '') or ''}",
            f"Phone             : {getattr(user, 'userAppId', '') or ''}",
            f"Email             : {getattr(user, 'emailId', '') or ''}",
            "",
            "AMOUNT",
            f"Transfer to vendor: {_rupees(int(row.amount_paise))}",
            f"Withdrawal fee    : {_rupees(int(row.fee_paise))} (kept by OpenBid)",
            f"Held from wallet  : {_rupees(total)}",
            "",
            "BANK ACCOUNT (registered by the vendor)",
            f"Account holder    : {row.bank_account_holder_name}",
            f"Account number    : {row.bank_account_no}",
            f"IFSC              : {row.bank_ifsc}",
            f"Bank              : {row.bank_name or ''}",
            f"Penny-drop check  : {row.bank_verification_note or 'Not checked'}",
            "",
            "WHAT TO DO",
            f"1. Transfer exactly {_rupees(int(row.amount_paise))} to the account above by IMPS/NEFT.",
            "   Check the account holder name matches the vendor before paying.",
            "2. After the transfer succeeds, mark it paid with the bank UTR:",
            f"   POST {api_base}/internal/wallet/withdrawals/{row.withdrawal_ref}/paid",
            '   Header X-OpenBid-Wallet-Ops-Key, body {"utr": "<UTR>"}',
            "   This debits the held amount from the vendor's wallet.",
            "3. If you cannot pay (details wrong, suspicious request), reject it:",
            f"   POST {api_base}/internal/wallet/withdrawals/{row.withdrawal_ref}/rejected",
            '   Header X-OpenBid-Wallet-Ops-Key, body {"reason": "<reason shown to vendor>"}',
            "   This returns the held amount to the vendor's available balance.",
            "",
            "Until one of these is done, the amount stays on hold in the vendor's wallet.",
            "Pending requests: GET /internal/wallet/withdrawals?status=REQUESTED",
            "",
            "This mailbox contains bank details. Do not forward outside the accounts team.",
        ]
    )


def _notify_ops(db: Session, row: VendorWalletWithdrawal, user: User) -> None:
    to_address = wallet_notifications.ops_email_address()
    from_address = wallet_notifications.ops_email_from()
    try:
        result = send_email(
            message=_ops_email_body(row, user),
            subject=f"[OpenBid] Wallet withdrawal {row.withdrawal_ref} - {_rupees(int(row.amount_paise))}",
            from_address=from_address,
            from_name="OpenBid Wallet",
            to_address=to_address,
            to_name="OpenBid Accounts",
            is_html=False,
        )
        sent = isinstance(result, dict) and result.get("message") == "SENT"
    except Exception:
        sent = False
    if not sent:
        logger.error("WALLET_WITHDRAWAL_OPS_EMAIL_FAILED ref=%s", row.withdrawal_ref)
    try:
        row.ops_email_status = "SENT" if sent else "FAILED"
        db.commit()
    except SQLAlchemyError:
        db.rollback()


# --- Ops actions (internal key) ----------------------------------------------


def _ops_item(row: VendorWalletWithdrawal, user: Optional[User]) -> OpsWithdrawalItem:
    base = _to_item(row).model_dump()
    return OpsWithdrawalItem(
        **base,
        vendorName=getattr(user, "fullName", None) if user else None,
        vendorPhone=getattr(user, "userAppId", None) if user else None,
        accountHolderName=row.bank_account_holder_name,
        accountNumber=row.bank_account_no,
        bankIFSC=row.bank_ifsc,
        opsEmailStatus=row.ops_email_status,
        bankVerification=row.bank_verification_note,
    )


def ops_list_withdrawals(db: Session, status_filter: Optional[str]) -> OpsWithdrawalList:
    query = db.query(VendorWalletWithdrawal)
    if status_filter:
        query = query.filter(VendorWalletWithdrawal.status == status_filter.upper())
    rows: List[VendorWalletWithdrawal] = (
        query.order_by(VendorWalletWithdrawal.id.desc()).limit(100).all()
    )
    uids = {int(r.user_uid) for r in rows}
    users = {int(u.UID): u for u in db.query(User).filter(User.UID.in_(uids)).all()} if uids else {}
    return OpsWithdrawalList(items=[_ops_item(r, users.get(int(r.user_uid))) for r in rows])


def _lock_withdrawal(db: Session, withdrawal_ref: str) -> VendorWalletWithdrawal:
    row = (
        db.query(VendorWalletWithdrawal)
        .filter(VendorWalletWithdrawal.withdrawal_ref == withdrawal_ref)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="WITHDRAWAL_NOT_FOUND")
    return row


def ops_mark_paid(
    db: Session,
    withdrawal_ref: str,
    utr: str,
    background_tasks: Optional[BackgroundTasks] = None,
) -> OpsWithdrawalItem:
    utr = utr.strip().upper()
    try:
        row = _lock_withdrawal(db, withdrawal_ref)
        if row.status == STATUS_PAID and row.utr == utr:
            db.commit()
            return _ops_item(row, None)
        if row.status != STATUS_REQUESTED:
            db.rollback()
            raise HTTPException(status_code=409, detail="WITHDRAWAL_NOT_PENDING")
        uid = int(row.user_uid)
        apply_wallet_entry(
            db,
            user_uid=uid,
            direction=DIRECTION_DEBIT,
            txn_type=TXN_TYPE_WITHDRAWAL,
            amount_paise=int(row.amount_paise),
            idempotency_key=f"wd:{row.withdrawal_ref}:amount",
            description=f"Withdrawal to bank {_mask_bank_account_mobile(row.bank_account_no)} (UTR {utr})",
            reference_id=row.withdrawal_ref,
            consume_hold_paise=int(row.amount_paise),
        )
        if int(row.fee_paise) > 0:
            apply_wallet_entry(
                db,
                user_uid=uid,
                direction=DIRECTION_DEBIT,
                txn_type=TXN_TYPE_WITHDRAWAL_FEE,
                amount_paise=int(row.fee_paise),
                idempotency_key=f"wd:{row.withdrawal_ref}:fee",
                description="Withdrawal fee",
                reference_id=row.withdrawal_ref,
                consume_hold_paise=int(row.fee_paise),
            )
        row.status = STATUS_PAID
        row.utr = utr
        row.resolved_at = _ist_now_naive()
        db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("ops_paid") from None
    logger.info("WALLET_WITHDRAWAL_PAID ref=%s", withdrawal_ref)
    _push(
        background_tasks,
        int(row.user_uid),
        "Withdrawal paid",
        f"{_rupees(int(row.amount_paise))} has been transferred to your bank account (UTR {utr}).",
    )
    return _ops_item(row, None)


def ops_mark_rejected(
    db: Session,
    withdrawal_ref: str,
    reason: str,
    background_tasks: Optional[BackgroundTasks] = None,
) -> OpsWithdrawalItem:
    try:
        row = _lock_withdrawal(db, withdrawal_ref)
        if row.status != STATUS_REQUESTED:
            db.rollback()
            raise HTTPException(status_code=409, detail="WITHDRAWAL_NOT_PENDING")
        release_held_funds(
            db,
            user_uid=int(row.user_uid),
            amount_paise=int(row.amount_paise) + int(row.fee_paise),
        )
        row.status = STATUS_REJECTED
        row.rejection_reason = reason.strip()
        row.resolved_at = _ist_now_naive()
        db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("ops_rejected") from None
    logger.info("WALLET_WITHDRAWAL_REJECTED ref=%s", withdrawal_ref)
    _push(
        background_tasks,
        int(row.user_uid),
        "Withdrawal rejected",
        f"Your withdrawal of {_rupees(int(row.amount_paise))} was rejected and the amount is back in your wallet. Reason: {row.rejection_reason}",
    )
    return _ops_item(row, None)
