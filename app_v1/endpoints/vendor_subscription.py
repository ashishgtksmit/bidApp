"""Vendor subscription endpoints (PR63)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Path
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser, get_current_user
from ..auth.internal import require_internal_notification_access, require_wallet_ops_access
from ..crud import vendor_subscription as subscription_crud
from ..crud import vendor_subscription_renewal as renewal_crud
from ..database import get_db
from ..schemas.vendor_subscription import (
    OpsSubscriptionGrantRequest,
    OpsSubscriptionGrantResponse,
    OpsSubscriptionPlanItem,
    OpsSubscriptionPlanList,
    OpsSubscriptionPlanUpdate,
    SubscriptionDailyRunResult,
    VendorSubscriptionAutoRenewRequest,
    VendorSubscriptionCheckoutStatus,
    VendorSubscriptionHistory,
    VendorSubscriptionPurchaseRequest,
    VendorSubscriptionStatus,
)
from ..schemas.vendor_wallet import WalletRechargeOrder

router = APIRouter()


@router.get(
    "/vendor/subscription",
    response_model=VendorSubscriptionStatus,
    summary="Vendor subscription status, plan price and wallet balance (PR63)",
)
def vendor_subscription_status(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorSubscriptionStatus:
    """Approved, unlocked vendors only. ``plan`` is null when no active plan
    is configured; ``enabled`` / ``enforced`` mirror the server flags."""
    return subscription_crud.get_subscription_status(db, current_user)


@router.post(
    "/vendor/subscription/purchase",
    response_model=VendorSubscriptionStatus,
    summary="Pay the subscription from the wallet balance (PR63)",
)
def vendor_subscription_purchase(
    payload: VendorSubscriptionPurchaseRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorSubscriptionStatus:
    """422 ``INSUFFICIENT_BALANCE`` when the wallet is short. Same
    ``idempotencyKey`` never charges twice."""
    return subscription_crud.purchase_with_wallet(db, current_user, payload, background_tasks)


@router.post(
    "/vendor/subscription/checkout",
    response_model=WalletRechargeOrder,
    summary="Pay the subscription through Cashfree (PR63)",
)
def vendor_subscription_checkout(
    payload: VendorSubscriptionPurchaseRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WalletRechargeOrder:
    """Returns a Cashfree order for the plan price. When it is PAID the
    amount is credited to the wallet and immediately debited for the
    subscription."""
    return subscription_crud.create_checkout(db, current_user, payload)


@router.get(
    "/vendor/subscription/checkout/{orderId}",
    response_model=VendorSubscriptionCheckoutStatus,
    summary="Cashfree subscription payment status (PR63)",
)
def vendor_subscription_checkout_status(
    background_tasks: BackgroundTasks,
    orderId: str = Path(..., max_length=40),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorSubscriptionCheckoutStatus:
    return subscription_crud.get_checkout_status(db, current_user, orderId, background_tasks)


@router.put(
    "/vendor/subscription/auto-renew",
    response_model=VendorSubscriptionStatus,
    summary="Turn wallet auto-renew on or off (PR63)",
)
def vendor_subscription_auto_renew(
    payload: VendorSubscriptionAutoRenewRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorSubscriptionStatus:
    return subscription_crud.set_auto_renew(db, current_user, payload.enabled)


@router.get(
    "/vendor/subscription/history",
    response_model=VendorSubscriptionHistory,
    summary="Paid subscription periods, newest first (PR63)",
)
def vendor_subscription_history(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> VendorSubscriptionHistory:
    return subscription_crud.list_history(db, current_user)


# --- Internal -------------------------------------------------------------------


@router.post(
    "/internal/subscriptions/run-daily",
    response_model=SubscriptionDailyRunResult,
    dependencies=[Depends(require_internal_notification_access)],
    include_in_schema=False,
)
def internal_subscription_run_daily(db: Session = Depends(get_db)) -> SubscriptionDailyRunResult:
    """Worker timer: auto-renew from the wallet and send expiry reminders.
    Safe to call more than once a day."""
    return renewal_crud.run_daily(db)


@router.get(
    "/internal/subscriptions/plans",
    response_model=OpsSubscriptionPlanList,
    dependencies=[Depends(require_wallet_ops_access)],
    include_in_schema=False,
)
def internal_subscription_plans(db: Session = Depends(get_db)) -> OpsSubscriptionPlanList:
    return subscription_crud.ops_list_plans(db)


@router.put(
    "/internal/subscriptions/plans/{code}",
    response_model=OpsSubscriptionPlanItem,
    dependencies=[Depends(require_wallet_ops_access)],
    include_in_schema=False,
)
def internal_subscription_plan_upsert(
    payload: OpsSubscriptionPlanUpdate,
    code: str = Path(..., min_length=1, max_length=16, pattern=r"^[A-Z0-9_]+$"),
    db: Session = Depends(get_db),
) -> OpsSubscriptionPlanItem:
    """Change price / length / active, or create a plan. Applies to new
    purchases and renewals immediately."""
    return subscription_crud.ops_upsert_plan(db, code, payload)


@router.post(
    "/internal/subscriptions/grant",
    response_model=OpsSubscriptionGrantResponse,
    dependencies=[Depends(require_wallet_ops_access)],
    include_in_schema=False,
)
def internal_subscription_grant(
    payload: OpsSubscriptionGrantRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> OpsSubscriptionGrantResponse:
    return subscription_crud.ops_grant(db, payload, background_tasks)
