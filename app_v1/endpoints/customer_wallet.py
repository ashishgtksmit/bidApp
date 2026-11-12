"""Customer-side wallet endpoints.

The same wallet as ``/vendor/wallet/*`` (one wallet per user). These routes
accept any unlocked user and tag recharges / withdrawals as ``CUSTOMER``.
Request and response shapes are identical to the vendor routes.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser, get_current_user
from ..crud import vendor_wallet_recharge as recharge_crud
from ..crud import vendor_wallet_statement as statement_crud
from ..crud import vendor_wallet_withdrawal as withdrawal_crud
from ..crud.vendor_wallet import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SIDE_CUSTOMER,
    get_wallet_summary,
    list_wallet_transactions,
)
from ..database import get_db
from ..schemas.vendor_wallet import (
    VendorWalletSummary,
    VendorWalletTransactionPage,
    WalletRechargeCreateRequest,
    WalletRechargeOrder,
    WalletStatementRequest,
    WalletStatementResponse,
    WalletWithdrawalCreateRequest,
    WalletWithdrawalItem,
    WalletWithdrawalList,
    WalletWithdrawalOtpResponse,
    WalletWithdrawalPreflight,
)

router = APIRouter()


@router.get(
    "/customer/wallet",
    response_model=VendorWalletSummary,
    summary="Wallet balance from the customer side",
)
def customer_wallet_summary(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorWalletSummary:
    """JWT-owned balance in paise (same wallet as /vendor/wallet). Any unlocked user."""
    return get_wallet_summary(db, current_user, side=SIDE_CUSTOMER)


@router.get(
    "/customer/wallet/transactions",
    response_model=VendorWalletTransactionPage,
    summary="Wallet history from the customer side",
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
    """Newest-first ledger page with entries from both app sides (``source``)."""
    return list_wallet_transactions(
        db,
        current_user,
        side=SIDE_CUSTOMER,
        limit=limit,
        cursor=cursor,
        start_date=startDate,
        end_date=endDate,
    )


@router.post(
    "/customer/wallet/recharges",
    response_model=WalletRechargeOrder,
    summary="Create a Cashfree wallet recharge order from the customer side",
)
def customer_create_wallet_recharge(
    payload: WalletRechargeCreateRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletRechargeOrder:
    return recharge_crud.create_recharge(db, current_user, payload, side=SIDE_CUSTOMER)


@router.get(
    "/customer/wallet/recharges/{orderId}",
    response_model=WalletRechargeOrder,
    summary="Recharge order status from the customer side",
)
def customer_get_wallet_recharge(
    background_tasks: BackgroundTasks,
    orderId: str = Path(..., max_length=40),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletRechargeOrder:
    return recharge_crud.get_recharge_status(
        db, current_user, orderId, background_tasks, side=SIDE_CUSTOMER
    )


@router.get(
    "/customer/wallet/withdrawals/preflight",
    response_model=WalletWithdrawalPreflight,
    summary="Bank details, limits and fees before a withdrawal (customer side)",
)
def customer_wallet_withdrawal_preflight(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalPreflight:
    return withdrawal_crud.get_withdrawal_preflight(db, current_user, side=SIDE_CUSTOMER)


@router.post(
    "/customer/wallet/withdrawals/otp",
    response_model=WalletWithdrawalOtpResponse,
    summary="Send withdrawal confirmation OTP by SMS (customer side)",
)
def customer_wallet_withdrawal_otp(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalOtpResponse:
    """403 ``VENDOR_ACCESS_RESTRICTED`` for vendor applicants not yet approved."""
    return withdrawal_crud.send_withdrawal_otp(db, current_user, side=SIDE_CUSTOMER)


@router.post(
    "/customer/wallet/withdrawals",
    response_model=WalletWithdrawalItem,
    summary="Request a withdrawal to the registered bank account (customer side)",
)
def customer_create_wallet_withdrawal(
    payload: WalletWithdrawalCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalItem:
    return withdrawal_crud.create_withdrawal(
        db, current_user, payload, background_tasks, side=SIDE_CUSTOMER
    )


@router.get(
    "/customer/wallet/withdrawals",
    response_model=WalletWithdrawalList,
    summary="Recent withdrawal requests (customer side)",
)
def customer_list_wallet_withdrawals(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalList:
    return withdrawal_crud.list_withdrawals(db, current_user, side=SIDE_CUSTOMER)


@router.post(
    "/customer/wallet/statements",
    response_model=WalletStatementResponse,
    summary="Email a CSV wallet statement (customer side)",
)
def customer_email_wallet_statement(
    payload: WalletStatementRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletStatementResponse:
    return statement_crud.email_wallet_statement(db, current_user, payload, side=SIDE_CUSTOMER)
