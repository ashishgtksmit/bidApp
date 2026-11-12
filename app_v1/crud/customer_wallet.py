"""PR50 customer wallet read CRUD (balance + transaction history).

JWT-owned via AuthenticatedUser.uid. Any user whose account is not locked,
vendors included; the customer wallet is separate from the vendor wallet.
Read paths never create wallet rows; a missing wallet reads as zero.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..models.customer_wallet import CustomerWallet, CustomerWalletTransaction
from ..models.user_table import User
from ..schemas.vendor_wallet import VendorWalletSummary, VendorWalletTransactionPage
from .vendor_wallet import (
    DEFAULT_PAGE_SIZE,
    read_wallet_summary,
    read_wallet_transactions,
)


def _load_eligible_customer(db: Session, current_user: AuthenticatedUser) -> User:
    user = db.query(User).filter(User.UID == current_user.uid).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="USER_NOT_FOUND",
        )
    if bool(getattr(user, "lockApp", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ACCOUNT_LOCKED",
        )
    return user


def get_customer_wallet_summary(
    db: Session,
    current_user: AuthenticatedUser,
) -> VendorWalletSummary:
    return read_wallet_summary(
        db,
        current_user,
        wallet_model=CustomerWallet,
        load_owner=_load_eligible_customer,
    )


def list_customer_wallet_transactions(
    db: Session,
    current_user: AuthenticatedUser,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> VendorWalletTransactionPage:
    return read_wallet_transactions(
        db,
        current_user,
        txn_model=CustomerWalletTransaction,
        load_owner=_load_eligible_customer,
        limit=limit,
        cursor=cursor,
        start_date=start_date,
        end_date=end_date,
    )
