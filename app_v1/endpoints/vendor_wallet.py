"""Vendor wallet endpoints: balance + history (PR47), recharge, withdrawal,
Cashfree webhook and ops actions (PR48)."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser, get_current_user
from ..auth.internal import require_wallet_ops_access
from ..crud.vendor_wallet import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    get_vendor_wallet_summary,
    list_vendor_wallet_transactions,
)
from ..crud import vendor_wallet_recharge as recharge_crud
from ..crud import vendor_wallet_statement as statement_crud
from ..crud import vendor_wallet_withdrawal as withdrawal_crud
from ..crud.user import _mask_bank_account_mobile
from ..crud.vendor_bank_security import send_bank_change_otp
from ..database import get_db
from ..models.user_table import User
from ..schemas.vendor_wallet import (
    OtpSentResponse,
    OpsWithdrawalItem,
    OpsWithdrawalList,
    OpsWithdrawalPaidRequest,
    OpsWithdrawalRejectRequest,
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
from ..services import wallet_ops_link_html, wallet_ops_links

router = APIRouter()

_WITHDRAWAL_REF = r"^OBWD[0-9A-F]{20}$"


@router.get(
    "/vendor/wallet",
    response_model=VendorWalletSummary,
    summary="Vendor wallet balance (PR47)",
)
def vendor_wallet_summary(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorWalletSummary:
    """JWT-owned wallet balance in paise. Approved, unlocked vendors only."""
    return get_vendor_wallet_summary(db, current_user)


@router.get(
    "/vendor/wallet/transactions",
    response_model=VendorWalletTransactionPage,
    summary="Vendor wallet transaction history (PR47)",
)
def vendor_wallet_transactions(
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
    """JWT-owned newest-first ledger page. Approved, unlocked vendors only."""
    return list_vendor_wallet_transactions(
        db,
        current_user,
        limit=limit,
        cursor=cursor,
        start_date=startDate,
        end_date=endDate,
    )


# --- Recharge (PR48) ---------------------------------------------------------


@router.post(
    "/vendor/wallet/recharges",
    response_model=WalletRechargeOrder,
    summary="Create a Cashfree wallet recharge order (PR48)",
)
def create_wallet_recharge(
    payload: WalletRechargeCreateRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletRechargeOrder:
    """Returns ``paymentSessionId`` for the Cashfree SDK. Same
    ``idempotencyKey`` returns the same order. The wallet is credited only
    after the server verifies the order with Cashfree."""
    return recharge_crud.create_recharge(db, current_user, payload)


@router.get(
    "/vendor/wallet/recharges/{orderId}",
    response_model=WalletRechargeOrder,
    summary="Recharge order status (PR48)",
)
def get_wallet_recharge(
    background_tasks: BackgroundTasks,
    orderId: str = Path(..., max_length=40),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletRechargeOrder:
    """Re-checks Cashfree while the order is pending; credits when PAID."""
    return recharge_crud.get_recharge_status(db, current_user, orderId, background_tasks)


@router.post("/webhooks/cashfree/wallet", include_in_schema=False)
async def cashfree_wallet_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Public Cashfree PG webhook. Signature-verified; body is only a trigger
    to re-fetch the order."""
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
    except (UnicodeDecodeError, ValueError):
        payload = None
    status_code = await run_in_threadpool(
        recharge_crud.handle_cashfree_webhook,
        db,
        raw_body=raw_body,
        timestamp=request.headers.get("x-webhook-timestamp"),
        signature=request.headers.get("x-webhook-signature"),
        payload=payload,
        background_tasks=background_tasks,
    )
    return JSONResponse(
        status_code=status_code,
        content={"ok": status_code == 200},
        background=background_tasks,
    )


# --- Withdrawal (PR48) -------------------------------------------------------


@router.get(
    "/vendor/wallet/withdrawals/preflight",
    response_model=WalletWithdrawalPreflight,
    summary="Bank details, limits and fees before a withdrawal (PR48)",
)
def wallet_withdrawal_preflight(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalPreflight:
    return withdrawal_crud.get_withdrawal_preflight(db, current_user)


@router.post(
    "/vendor/wallet/withdrawals/otp",
    response_model=WalletWithdrawalOtpResponse,
    summary="Send withdrawal confirmation OTP by SMS (PR48)",
)
def wallet_withdrawal_otp(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalOtpResponse:
    """409 ``BANK_DETAILS_REQUIRED`` when no bank account is registered."""
    return withdrawal_crud.send_withdrawal_otp(db, current_user)


@router.post(
    "/vendor/wallet/withdrawals",
    response_model=WalletWithdrawalItem,
    summary="Request a withdrawal to the registered bank account (PR48)",
)
def create_wallet_withdrawal(
    payload: WalletWithdrawalCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalItem:
    """Holds amount + fee and emails the accounts team for manual payout."""
    return withdrawal_crud.create_withdrawal(db, current_user, payload, background_tasks)


@router.get(
    "/vendor/wallet/withdrawals",
    response_model=WalletWithdrawalList,
    summary="Recent withdrawal requests (PR48)",
)
def list_wallet_withdrawals(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletWithdrawalList:
    return withdrawal_crud.list_withdrawals(db, current_user)


# --- Statement / bank-change OTP (PR49) --------------------------------------


@router.post(
    "/vendor/wallet/statements",
    response_model=WalletStatementResponse,
    summary="Email a CSV wallet statement to the registered email (PR49)",
)
def email_wallet_statement(
    payload: WalletStatementRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletStatementResponse:
    return statement_crud.email_wallet_statement(db, current_user, payload)


@router.post(
    "/vendor/bank-details/otp",
    response_model=OtpSentResponse,
    summary="Send OTP for changing bank details (PR49)",
)
def bank_change_otp(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> OtpSentResponse:
    """Use the OTP as ``otp`` in PUT /updatevendorbankdetails when the server
    answers 422 ``BANK_CHANGE_OTP_REQUIRED``."""
    expires_in = send_bank_change_otp(db, current_user)
    return OtpSentResponse(message="OTP_SENT", expiresInSeconds=expires_in)


# --- Ops (internal key only, PR48) -----------------------------------------


@router.get(
    "/internal/wallet/withdrawals",
    response_model=OpsWithdrawalList,
    dependencies=[Depends(require_wallet_ops_access)],
    include_in_schema=False,
)
def ops_list_wallet_withdrawals(
    status: Optional[str] = Query(None, pattern=r"^(REQUESTED|PAID|REJECTED)$"),
    db: Session = Depends(get_db),
) -> OpsWithdrawalList:
    return withdrawal_crud.ops_list_withdrawals(db, status)


@router.post(
    "/internal/wallet/withdrawals/{withdrawalId}/paid",
    response_model=OpsWithdrawalItem,
    dependencies=[Depends(require_wallet_ops_access)],
    include_in_schema=False,
)
def ops_mark_wallet_withdrawal_paid(
    payload: OpsWithdrawalPaidRequest,
    background_tasks: BackgroundTasks,
    withdrawalId: str = Path(..., pattern=_WITHDRAWAL_REF),
    db: Session = Depends(get_db),
) -> OpsWithdrawalItem:
    return withdrawal_crud.ops_mark_paid(db, withdrawalId, payload.utr, background_tasks)


@router.post(
    "/internal/wallet/withdrawals/{withdrawalId}/rejected",
    response_model=OpsWithdrawalItem,
    dependencies=[Depends(require_wallet_ops_access)],
    include_in_schema=False,
)
def ops_mark_wallet_withdrawal_rejected(
    payload: OpsWithdrawalRejectRequest,
    background_tasks: BackgroundTasks,
    withdrawalId: str = Path(..., pattern=_WITHDRAWAL_REF),
    db: Session = Depends(get_db),
) -> OpsWithdrawalItem:
    return withdrawal_crud.ops_mark_rejected(db, withdrawalId, payload.reason, background_tasks)


_OPS_LINK_ACTION = r"^(paid|rejected)$"


def _ops_link_detail_message(detail: object) -> str:
    code = detail if isinstance(detail, str) else "OPS_LINK_INVALID"
    return {
        "OPS_LINK_INVALID": "This link is invalid or has been tampered with.",
        "OPS_LINK_EXPIRED": "This link has expired. Use the ops API with the wallet ops key, or ask for a new withdrawal email.",
        "OPS_LINK_USED": "This link was already used.",
        "WITHDRAWAL_NOT_FOUND": "Withdrawal not found.",
        "WITHDRAWAL_NOT_PENDING": "This withdrawal is no longer pending.",
        "UTR_REQUIRED": "Enter the bank UTR.",
        "REASON_REQUIRED": "Enter a rejection reason (at least 3 characters).",
        "WALLET_OPS_LINKS_UNAVAILABLE": "Ops links are not configured on the server.",
    }.get(str(code), str(code))


def _ops_form_context(db: Session, withdrawal_id: str, action: str, token: str):
    row = withdrawal_crud.load_withdrawal_for_ops_link(db, withdrawal_id)
    wallet_ops_links.verify_ops_link_against_row(
        row,
        withdrawal_ref=withdrawal_id,
        action=action,  # type: ignore[arg-type]
        token=token,
    )
    if row.status != withdrawal_crud.STATUS_REQUESTED:
        raise HTTPException(status_code=409, detail="WITHDRAWAL_NOT_PENDING")
    user = db.query(User).filter(User.UID == int(row.user_uid)).first()
    amount_label = (
        f"Transfer {withdrawal_crud._rupees(int(row.amount_paise))} "
        f"(fee {withdrawal_crud._rupees(int(row.fee_paise))})"
    )
    return row, user, amount_label


@router.get(
    "/internal/wallet/withdrawals/{withdrawalId}/ops/{action}",
    include_in_schema=False,
    response_class=HTMLResponse,
)
def ops_wallet_withdrawal_link_form(
    withdrawalId: str = Path(..., pattern=_WITHDRAWAL_REF),
    action: str = Path(..., pattern=_OPS_LINK_ACTION),
    t: str = Query(..., min_length=20),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        _row, user, amount_label = _ops_form_context(db, withdrawalId, action, t)
    except HTTPException as exc:
        return HTMLResponse(
            wallet_ops_link_html.error_page(
                "OpenBid wallet ops",
                _ops_link_detail_message(exc.detail),
            ),
            status_code=exc.status_code if exc.status_code in (403, 404, 409, 410) else 400,
        )
    return HTMLResponse(
        wallet_ops_link_html.form_page(
            action=action,
            withdrawal_id=withdrawalId,
            amount_label=amount_label,
            vendor_name=str(getattr(user, "fullName", "") or "") if user else "",
            masked_account=_mask_bank_account_mobile(_row.bank_account_no),
            token=t,
            form_action_url=f"/internal/wallet/withdrawals/{withdrawalId}/ops/{action}",
        )
    )


@router.post(
    "/internal/wallet/withdrawals/{withdrawalId}/ops/{action}",
    include_in_schema=False,
    response_class=HTMLResponse,
)
def ops_wallet_withdrawal_link_submit(
    background_tasks: BackgroundTasks,
    withdrawalId: str = Path(..., pattern=_WITHDRAWAL_REF),
    action: str = Path(..., pattern=_OPS_LINK_ACTION),
    t: str = Form(...),
    utr: Optional[str] = Form(None),
    reason: Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        if action == wallet_ops_links.ACTION_PAID:
            # Match OpsWithdrawalPaidRequest constraints.
            cleaned = (utr or "").strip()
            if len(cleaned) < 6 or len(cleaned) > 40 or not cleaned.isalnum():
                raise HTTPException(status_code=422, detail="UTR_REQUIRED")
            item = withdrawal_crud.ops_resolve_via_link(
                db,
                withdrawal_ref=withdrawalId,
                action=action,
                token=t,
                utr=cleaned,
                background_tasks=background_tasks,
            )
            return HTMLResponse(
                wallet_ops_link_html.success_page(
                    "Withdrawal marked paid",
                    f"{item.withdrawalId} is paid. UTR {item.utr}.",
                )
            )
        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 3:
            raise HTTPException(status_code=422, detail="REASON_REQUIRED")
        item = withdrawal_crud.ops_resolve_via_link(
            db,
            withdrawal_ref=withdrawalId,
            action=action,
            token=t,
            reason=cleaned_reason,
            background_tasks=background_tasks,
        )
        return HTMLResponse(
            wallet_ops_link_html.success_page(
                "Withdrawal rejected",
                f"{item.withdrawalId} was rejected. Held funds were released.",
            )
        )
    except HTTPException as exc:
        # Re-show the form when validation fails but the token is still valid.
        if exc.status_code == 422:
            try:
                row, user, amount_label = _ops_form_context(db, withdrawalId, action, t)
                return HTMLResponse(
                    wallet_ops_link_html.form_page(
                        action=action,
                        withdrawal_id=withdrawalId,
                        amount_label=amount_label,
                        vendor_name=str(getattr(user, "fullName", "") or "") if user else "",
                        masked_account=_mask_bank_account_mobile(row.bank_account_no),
                        token=t,
                        form_action_url=f"/internal/wallet/withdrawals/{withdrawalId}/ops/{action}",
                        error=_ops_link_detail_message(exc.detail),
                    ),
                    status_code=422,
                )
            except HTTPException:
                pass
        return HTMLResponse(
            wallet_ops_link_html.error_page(
                "OpenBid wallet ops",
                _ops_link_detail_message(exc.detail),
            ),
            status_code=exc.status_code if exc.status_code in (403, 404, 409, 410, 422) else 400,
        )
