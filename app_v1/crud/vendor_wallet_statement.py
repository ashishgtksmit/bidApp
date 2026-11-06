"""PR49 wallet statement: CSV of ledger rows emailed to the vendor's
registered email address. Same date rules as GET /vendor/wallet/transactions
(yyyy-MM-dd inclusive, Asia/Kolkata, max 24 months)."""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..crud.vendor_earnings import _calendar_months_spanned, _parse_report_date
from ..crud.vendor_wallet import MAX_RANGE_MONTHS, _load_eligible_vendor
from ..models.vendor_wallet import VendorWalletTransaction
from ..schemas.vendor_wallet import WalletStatementRequest, WalletStatementResponse
from ..services.wallet_notifications import rupees
from ..utils.email import send_email
from ..utils.rate_limit import enforce_rate_limit

logger = logging.getLogger(__name__)

MAX_STATEMENT_ROWS = 5000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TYPE_LABELS = {
    "RECHARGE": "Wallet Recharge",
    "WITHDRAWAL": "Withdrawal to Bank",
    "WITHDRAWAL_FEE": "Withdrawal Fee",
    "WITHDRAWAL_REVERSAL": "Withdrawal Reversed",
    "ADJUSTMENT": "Adjustment",
}


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _csv_cell(value: str) -> str:
    """Neutralise spreadsheet formulas (CSV injection)."""
    text = str(value or "")
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _amount(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def email_wallet_statement(
    db: Session,
    current_user: AuthenticatedUser,
    payload: WalletStatementRequest,
) -> WalletStatementResponse:
    start = _parse_report_date(payload.startDate, "startDate")
    end = _parse_report_date(payload.endDate, "endDate")
    if start > end:
        raise HTTPException(status_code=422, detail="startDate must be less than or equal to endDate.")
    if _calendar_months_spanned(start, end) > MAX_RANGE_MONTHS:
        raise HTTPException(status_code=422, detail="WALLET_RANGE_TOO_LARGE")

    try:
        user = _load_eligible_vendor(db, current_user)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="WALLET_QUERY_FAILED") from None
    email = str(getattr(user, "emailId", "") or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="EMAIL_NOT_AVAILABLE")

    limited = enforce_rate_limit(
        db,
        bucket_key=f"wallet_statement:uid:{current_user.uid}",
        max_hits=int(os.getenv("RATE_LIMIT_WALLET_STATEMENT_PER_USER", "5")),
        window_seconds=int(os.getenv("RATE_LIMIT_WALLET_STATEMENT_WINDOW_SECONDS", "3600")),
    )
    if limited is not None:
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    try:
        rows = (
            db.query(VendorWalletTransaction)
            .filter(
                VendorWalletTransaction.user_uid == current_user.uid,
                VendorWalletTransaction.created_at >= datetime.combine(start, time.min),
                VendorWalletTransaction.created_at
                < datetime.combine(end + timedelta(days=1), time.min),
            )
            .order_by(VendorWalletTransaction.id.asc())
            .limit(MAX_STATEMENT_ROWS + 1)
            .all()
        )
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="WALLET_QUERY_FAILED") from None
    if len(rows) > MAX_STATEMENT_ROWS:
        raise HTTPException(status_code=422, detail="STATEMENT_TOO_LARGE")

    credits = sum(int(r.amount_paise) for r in rows if r.direction == "CREDIT")
    debits = sum(int(r.amount_paise) for r in rows if r.direction == "DEBIT")
    period = f"{start.isoformat()} to {end.isoformat()}"
    workdir = tempfile.mkdtemp(prefix="obw-stmt-")
    path = os.path.join(workdir, f"OpenBid_Wallet_Statement_{start.isoformat()}_{end.isoformat()}.csv")
    try:
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Date (IST)", "Transaction ID", "Type", "Description", "Credit (INR)", "Debit (INR)", "Balance After (INR)", "Status"]
            )
            for r in rows:
                amount = _amount(int(r.amount_paise))
                writer.writerow(
                    [
                        r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                        r.txn_ref,
                        _TYPE_LABELS.get(r.txn_type, r.txn_type),
                        _csv_cell(r.description or ""),
                        amount if r.direction == "CREDIT" else "",
                        amount if r.direction == "DEBIT" else "",
                        _amount(int(r.balance_after_paise)),
                        r.status,
                    ]
                )
        body = "\n".join(
            [
                f"Dear {getattr(user, 'fullName', '') or 'Vendor'},",
                "",
                f"Your OpenBid wallet statement for {period} is attached.",
                "",
                f"Transactions : {len(rows)}",
                f"Total credit : {rupees(credits)}",
                f"Total debit  : {rupees(debits)}",
                "",
                "If you did not request this statement, please contact OpenBid support.",
                "",
                "Team OpenBid",
            ]
        )
        result = send_email(
            message=body,
            subject=f"Your OpenBid wallet statement ({period})",
            from_address=(os.getenv("WALLET_STATEMENT_EMAIL_FROM") or "reservations@wizzride.com").strip(),
            from_name="OpenBid Wallet",
            to_address=email,
            to_name=str(getattr(user, "fullName", "") or "Vendor"),
            attachment_path=path,
            is_html=False,
        )
    except OSError:
        logger.error("WALLET_STATEMENT_FILE_FAILED uid=%s", current_user.uid)
        raise HTTPException(status_code=500, detail="STATEMENT_FAILED") from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not (isinstance(result, dict) and result.get("message") == "SENT"):
        logger.error("WALLET_STATEMENT_EMAIL_FAILED uid=%s", current_user.uid)
        raise HTTPException(status_code=502, detail="STATEMENT_EMAIL_FAILED")
    return WalletStatementResponse(
        message="STATEMENT_SENT",
        emailMasked=_mask_email(email),
        transactionCount=len(rows),
    )
