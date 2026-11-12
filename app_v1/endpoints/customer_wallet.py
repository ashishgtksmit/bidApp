"""Customer wallet endpoints: balance + history (PR50).

Separate from the vendor wallet (``/vendor/wallet``). Response shapes are the
same as the vendor wallet's.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser, get_current_user
from ..crud.customer_wallet import (
    get_customer_wallet_summary,
    list_customer_wallet_transactions,
)
from ..crud.vendor_wallet import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from ..database import get_db
from ..schemas.vendor_wallet import VendorWalletSummary, VendorWalletTransactionPage

router = APIRouter()


@router.get(
    "/customer/wallet",
    response_model=VendorWalletSummary,
    summary="Customer wallet balance (PR50)",
)
def customer_wallet_summary(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorWalletSummary:
    """JWT-owned customer wallet balance in paise. Any unlocked user."""
    return get_customer_wallet_summary(db, current_user)


@router.get(
    "/customer/wallet/transactions",
    response_model=VendorWalletTransactionPage,
    summary="Customer wallet transaction history (PR50)",
)
def customer_wallet_transactions(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(
        None,
        description="Opaque nextCursor from the previous page.",
    ),
    startDate: Optional[str] = Query(
        None,
        description="Inclusive Asia/Kolkata start date (yyyy-MM-dd). Require with endDate.",
    ),
    endDate: Optional[str] = Query(
        None,
        description="Inclusive Asia/Kolkata end date (yyyy-MM-dd). Require with startDate.",
    ),
) -> VendorWalletTransactionPage:
    """JWT-owned newest-first customer ledger page. Any unlocked user."""
    return list_customer_wallet_transactions(
        db,
        current_user,
        limit=limit,
        cursor=cursor,
        start_date=startDate,
        end_date=endDate,
    )
