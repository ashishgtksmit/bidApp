"""PR47 vendor wallet read CRUD (balance + transaction history).

JWT-owned via AuthenticatedUser.uid. Approved, unlocked vendors only.
Read paths never create wallet rows; a missing wallet reads as zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Callable, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..crud.user import _enforce_approved_vendor_eligibility
from ..crud.vendor_earnings import _calendar_months_spanned, _parse_report_date
from ..models.user_table import User
from ..models.vendor_wallet import VendorWallet, VendorWalletTransaction
from ..schemas.vendor_wallet import (
    VendorWalletSummary,
    VendorWalletTransactionItem,
    VendorWalletTransactionPage,
)
from ..services.vendor_wallet_ledger import CURRENCY

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_RANGE_MONTHS = 24


def _load_eligible_vendor(db: Session, current_user: AuthenticatedUser) -> User:
    user = db.query(User).filter(User.UID == current_user.uid).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="USER_NOT_FOUND",
        )
    _enforce_approved_vendor_eligibility(user)
    return user


def get_vendor_wallet_summary(
    db: Session,
    current_user: AuthenticatedUser,
) -> VendorWalletSummary:
    return read_wallet_summary(
        db, current_user, wallet_model=VendorWallet, load_owner=_load_eligible_vendor
    )


def read_wallet_summary(
    db: Session,
    current_user: AuthenticatedUser,
    *,
    wallet_model,
    load_owner: Callable[[Session, AuthenticatedUser], User],
) -> VendorWalletSummary:
    """Shared by the vendor (PR47) and customer (PR50) wallets."""
    try:
        load_owner(db, current_user)
        wallet = (
            db.query(wallet_model)
            .filter(wallet_model.user_uid == current_user.uid)
            .first()
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.error("WALLET_QUERY_FAILED op=summary")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WALLET_QUERY_FAILED",
        ) from None

    if wallet is None:
        return VendorWalletSummary(
            balancePaise=0,
            heldPaise=0,
            availablePaise=0,
            currency=CURRENCY,
        )
    balance = int(wallet.balance_paise or 0)
    held = int(wallet.held_paise or 0)
    return VendorWalletSummary(
        balancePaise=balance,
        heldPaise=held,
        availablePaise=max(balance - held, 0),
        currency=wallet.currency or CURRENCY,
    )


def _parse_cursor(cursor: Optional[str]) -> Optional[int]:
    if cursor is None or str(cursor).strip() == "":
        return None
    text = str(cursor).strip()
    if not text.isdigit() or int(text) <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_CURSOR",
        )
    return int(text)


def _to_item(row) -> VendorWalletTransactionItem:
    created = row.created_at
    created_str = (
        created.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(created, datetime) else ""
    )
    return VendorWalletTransactionItem(
        transactionId=str(row.txn_ref),
        direction=str(row.direction),
        type=str(row.txn_type),
        status=str(row.status),
        amountPaise=int(row.amount_paise),
        balanceAfterPaise=int(row.balance_after_paise),
        description=row.description,
        createdAt=created_str,
    )


def list_vendor_wallet_transactions(
    db: Session,
    current_user: AuthenticatedUser,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> VendorWalletTransactionPage:
    """Newest-first ledger page. ``nextCursor`` is opaque to clients."""
    return read_wallet_transactions(
        db,
        current_user,
        txn_model=VendorWalletTransaction,
        load_owner=_load_eligible_vendor,
        limit=limit,
        cursor=cursor,
        start_date=start_date,
        end_date=end_date,
    )


def read_wallet_transactions(
    db: Session,
    current_user: AuthenticatedUser,
    *,
    txn_model,
    load_owner: Callable[[Session, AuthenticatedUser], User],
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> VendorWalletTransactionPage:
    """Shared by the vendor (PR47) and customer (PR50) wallets."""
    has_start = start_date is not None and str(start_date).strip() != ""
    has_end = end_date is not None and str(end_date).strip() != ""
    if has_start ^ has_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="startDate and endDate must both be supplied or both omitted.",
        )
    range_start = range_end = None
    if has_start and has_end:
        range_start = _parse_report_date(str(start_date).strip(), "startDate")
        range_end = _parse_report_date(str(end_date).strip(), "endDate")
        if range_start > range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="startDate must be less than or equal to endDate.",
            )
        if _calendar_months_spanned(range_start, range_end) > MAX_RANGE_MONTHS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="WALLET_RANGE_TOO_LARGE",
            )
    before_id = _parse_cursor(cursor)
    page_size = max(1, min(int(limit), MAX_PAGE_SIZE))

    try:
        load_owner(db, current_user)
        query = db.query(txn_model).filter(txn_model.user_uid == current_user.uid)
        if range_start is not None and range_end is not None:
            query = query.filter(
                txn_model.created_at >= datetime.combine(range_start, time.min),
                txn_model.created_at
                < datetime.combine(range_end + timedelta(days=1), time.min),
            )
        if before_id is not None:
            query = query.filter(txn_model.id < before_id)
        rows = (
            query.order_by(txn_model.id.desc())
            .limit(page_size + 1)
            .all()
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.error("WALLET_QUERY_FAILED op=transactions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WALLET_QUERY_FAILED",
        ) from None

    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = str(page[-1].id) if has_more and page else None
    return VendorWalletTransactionPage(
        items=[_to_item(row) for row in page],
        nextCursor=next_cursor,
    )
