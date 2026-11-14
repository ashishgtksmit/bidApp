"""PR63 vendor subscription.

Approved vendors browse for free; bid / car / driver writes need an active
subscription when ``VENDOR_SUBSCRIPTION_ENFORCED=1`` (``require_subscribed_vendor``).

Price and length come only from ``vendor_subscription_plans``. Payment is a
``SUBSCRIPTION`` debit on the shared wallet ledger, either straight from the
balance or right after a Cashfree recharge tagged ``purpose=SUB:<plan>``
(``apply_paid_order``). A new period starts at the later of now and the
current period end, so paying early never loses days.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..crud.vendor_wallet import SIDE_VENDOR, _load_eligible_vendor
from ..models.user_table import User
from ..models.vendor_subscription import (
    VendorSubscriptionPeriod,
    VendorSubscriptionPlan,
    VendorSubscriptionState,
)
from ..models.vendor_wallet import VendorWallet, VendorWalletRecharge
from ..schemas.vendor_subscription import (
    OpsSubscriptionGrantRequest,
    OpsSubscriptionGrantResponse,
    OpsSubscriptionPlanItem,
    OpsSubscriptionPlanList,
    OpsSubscriptionPlanUpdate,
    VendorSubscriptionCheckoutStatus,
    VendorSubscriptionHistory,
    VendorSubscriptionPeriodItem,
    VendorSubscriptionPlanInfo,
    VendorSubscriptionPurchaseRequest,
    VendorSubscriptionStatus,
)
from ..schemas.vendor_wallet import WalletRechargeCreateRequest
from ..services import wallet_notifications
from ..services.vendor_wallet_ledger import (
    DIRECTION_DEBIT,
    SOURCE_VENDOR,
    TXN_TYPE_SUBSCRIPTION,
    InsufficientWalletBalance,
    apply_wallet_entry,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PLAN_CODE = "MONTHLY"
PURPOSE_PREFIX = "SUB:"

METHOD_WALLET = "WALLET"
METHOD_CASHFREE = "CASHFREE"
METHOD_AUTO_RENEW = "AUTO_RENEW"
METHOD_OPS_GRANT = "OPS_GRANT"

STATUS_ACTIVE = "ACTIVE"
STATUS_EXPIRED = "EXPIRED"
STATUS_NONE = "NONE"

SUBSCRIPTION_REQUIRED = "SUBSCRIPTION_REQUIRED"
SUBSCRIPTION_NOTIFICATION_TYPE = "subscription"
SUBSCRIPTION_NOTIFICATION_URL = "//My_Subscription"
HISTORY_LIMIT = 24


def subscription_enabled() -> bool:
    return (os.getenv("VENDOR_SUBSCRIPTION_ENABLED") or "").strip() == "1"


def subscription_enforced() -> bool:
    return (os.getenv("VENDOR_SUBSCRIPTION_ENFORCED") or "").strip() == "1"


def _ist_now_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else None


def _date_label(value: datetime) -> str:
    return value.strftime("%d %b %Y")


def _db_error(op: str) -> HTTPException:
    logger.error("SUBSCRIPTION_DB_FAILED op=%s", op)
    return HTTPException(status_code=500, detail="SUBSCRIPTION_QUERY_FAILED")


def _require_enabled() -> None:
    if not subscription_enabled():
        raise HTTPException(status_code=503, detail="SUBSCRIPTION_DISABLED")


# --- Plans -------------------------------------------------------------------


def _find_plan(db: Session, code: str) -> Optional[VendorSubscriptionPlan]:
    return db.query(VendorSubscriptionPlan).filter(VendorSubscriptionPlan.code == code).first()


def load_purchasable_plan(db: Session, code: str) -> VendorSubscriptionPlan:
    plan = _find_plan(db, code)
    if plan is None or not bool(plan.active) or int(plan.price_paise or 0) <= 0:
        raise HTTPException(status_code=503, detail="SUBSCRIPTION_PLAN_NOT_CONFIGURED")
    return plan


def _plan_info(plan: Optional[VendorSubscriptionPlan]) -> Optional[VendorSubscriptionPlanInfo]:
    if plan is None or not bool(plan.active):
        return None
    return VendorSubscriptionPlanInfo(
        code=plan.code,
        name=plan.name,
        pricePaise=int(plan.price_paise),
        durationDays=int(plan.duration_days),
    )


# --- State -------------------------------------------------------------------


def _get_state(db: Session, user_uid: int) -> Optional[VendorSubscriptionState]:
    return (
        db.query(VendorSubscriptionState)
        .filter(VendorSubscriptionState.user_uid == int(user_uid))
        .first()
    )


def _lock_or_create_state(db: Session, user_uid: int) -> VendorSubscriptionState:
    state = (
        db.query(VendorSubscriptionState)
        .filter(VendorSubscriptionState.user_uid == int(user_uid))
        .with_for_update()
        .first()
    )
    if state is None:
        state = VendorSubscriptionState(user_uid=int(user_uid), auto_renew=True)
        db.add(state)
        db.flush()
    return state


def is_subscription_active(db: Session, user_uid: int, now: Optional[datetime] = None) -> bool:
    state = _get_state(db, user_uid)
    end = state.current_period_end if state is not None else None
    return end is not None and end > (now or _ist_now_naive())


def require_subscribed_vendor(db: Session, user: User) -> None:
    """403 ``SUBSCRIPTION_REQUIRED`` for vendor writes when enforcement is on.

    Call after the vendor gate (``require_active_vendor``). Reads, deleting a
    bid, rejecting a booking and fulfilling won trips must not call this."""
    if not subscription_enforced():
        return
    if not is_subscription_active(db, int(user.UID)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SUBSCRIPTION_REQUIRED)


def add_period(
    db: Session,
    *,
    user_uid: int,
    plan_code: str,
    days: int,
    method: str,
    idempotency_key: str,
    amount_paise: int = 0,
    wallet_txn_ref: Optional[str] = None,
    recharge_order_id: Optional[str] = None,
    note: Optional[str] = None,
) -> tuple[VendorSubscriptionPeriod, bool]:
    """Append a period after the current one (or from now). Returns
    ``(period, created)``; a replayed key returns the original. Flushes only."""
    existing = (
        db.query(VendorSubscriptionPeriod)
        .filter(VendorSubscriptionPeriod.idempotency_key == idempotency_key)
        .first()
    )
    if existing is not None:
        return existing, False
    state = _lock_or_create_state(db, user_uid)
    now = _ist_now_naive()
    start = state.current_period_end if state.current_period_end and state.current_period_end > now else now
    end = start + timedelta(days=int(days))
    period = VendorSubscriptionPeriod(
        user_uid=int(user_uid),
        plan_code=plan_code,
        amount_paise=int(amount_paise),
        period_start=start,
        period_end=end,
        payment_method=method,
        wallet_txn_ref=wallet_txn_ref,
        recharge_order_id=recharge_order_id,
        idempotency_key=idempotency_key,
        note=note,
        created_at=now,
    )
    db.add(period)
    state.current_period_end = end
    db.flush()
    return period, True


def _charge_wallet_and_add_period(
    db: Session,
    *,
    user_uid: int,
    plan: VendorSubscriptionPlan,
    amount_paise: int,
    method: str,
    idempotency_key: str,
    recharge_order_id: Optional[str] = None,
) -> tuple[VendorSubscriptionPeriod, bool]:
    txn = apply_wallet_entry(
        db,
        user_uid=int(user_uid),
        direction=DIRECTION_DEBIT,
        txn_type=TXN_TYPE_SUBSCRIPTION,
        amount_paise=int(amount_paise),
        idempotency_key=idempotency_key,
        description=f"Vendor subscription ({plan.name})",
        reference_id=recharge_order_id,
        source=SOURCE_VENDOR,
    )
    return add_period(
        db,
        user_uid=user_uid,
        plan_code=plan.code,
        days=int(plan.duration_days),
        method=method,
        idempotency_key=idempotency_key,
        amount_paise=amount_paise,
        wallet_txn_ref=txn.txn_ref,
        recharge_order_id=recharge_order_id,
    )


# --- Vendor API ---------------------------------------------------------------


def _wallet_available(db: Session, user_uid: int) -> int:
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == int(user_uid)).first()
    if wallet is None:
        return 0
    return max(int(wallet.balance_paise or 0) - int(wallet.held_paise or 0), 0)


def _build_status(db: Session, user_uid: int) -> VendorSubscriptionStatus:
    state = _get_state(db, user_uid)
    now = _ist_now_naive()
    end = state.current_period_end if state is not None else None
    active = end is not None and end > now
    if active:
        status_text = STATUS_ACTIVE
        days_left = max(1, math.ceil((end - now).total_seconds() / 86400))
    else:
        status_text = STATUS_EXPIRED if end is not None else STATUS_NONE
        days_left = 0
    return VendorSubscriptionStatus(
        enabled=subscription_enabled(),
        enforced=subscription_enforced(),
        active=active,
        status=status_text,
        currentPeriodEnd=_iso(end),
        daysLeft=days_left,
        autoRenew=bool(state.auto_renew) if state is not None else True,
        plan=_plan_info(_find_plan(db, DEFAULT_PLAN_CODE)),
        walletAvailablePaise=_wallet_available(db, user_uid),
    )


def get_subscription_status(db: Session, current_user: AuthenticatedUser) -> VendorSubscriptionStatus:
    try:
        _load_eligible_vendor(db, current_user)
        return _build_status(db, current_user.uid)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("status") from None


def purchase_with_wallet(
    db: Session,
    current_user: AuthenticatedUser,
    payload: VendorSubscriptionPurchaseRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> VendorSubscriptionStatus:
    """Debit the plan price from the wallet. 422 ``INSUFFICIENT_BALANCE``
    when the available balance is short (the app then offers Cashfree)."""
    _require_enabled()
    key = f"sub:{current_user.uid}:{payload.idempotencyKey}"
    created = False
    # Second attempt only after an IntegrityError from a concurrent first
    # purchase (wallet / state row creation, or the same key).
    for attempt in range(2):
        try:
            _load_eligible_vendor(db, current_user)
            plan = load_purchasable_plan(db, payload.planCode)
            _period, created = _charge_wallet_and_add_period(
                db,
                user_uid=current_user.uid,
                plan=plan,
                amount_paise=int(plan.price_paise),
                method=METHOD_WALLET,
                idempotency_key=key,
            )
            db.commit()
            break
        except HTTPException:
            db.rollback()
            raise
        except InsufficientWalletBalance:
            db.rollback()
            raise HTTPException(status_code=422, detail="INSUFFICIENT_BALANCE") from None
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise _db_error("purchase_race") from None
        except SQLAlchemyError:
            db.rollback()
            raise _db_error("purchase") from None
    if created:
        queue_activated_push(background_tasks, db, current_user.uid)
    return _build_status(db, current_user.uid)


def create_checkout(
    db: Session,
    current_user: AuthenticatedUser,
    payload: VendorSubscriptionPurchaseRequest,
):
    """Open a Cashfree order for the plan price (a wallet recharge tagged
    ``SUB:<plan>``). The subscription starts when the order is PAID."""
    from . import vendor_wallet_recharge as recharge_crud

    _require_enabled()
    try:
        _load_eligible_vendor(db, current_user)
        plan = load_purchasable_plan(db, payload.planCode)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("checkout") from None
    return recharge_crud.create_recharge(
        db,
        current_user,
        WalletRechargeCreateRequest(
            amountPaise=int(plan.price_paise),
            idempotencyKey=payload.idempotencyKey,
        ),
        side=SIDE_VENDOR,
        purpose=f"{PURPOSE_PREFIX}{plan.code}",
    )


def get_checkout_status(
    db: Session,
    current_user: AuthenticatedUser,
    order_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
) -> VendorSubscriptionCheckoutStatus:
    from . import vendor_wallet_recharge as recharge_crud

    try:
        row = (
            db.query(VendorWalletRecharge)
            .filter(
                VendorWalletRecharge.order_id == order_id,
                VendorWalletRecharge.user_uid == current_user.uid,
            )
            .first()
        )
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("checkout_lookup") from None
    if row is None or not str(row.purpose or "").startswith(PURPOSE_PREFIX):
        raise HTTPException(status_code=404, detail="RECHARGE_NOT_FOUND")
    order = recharge_crud.get_recharge_status(
        db, current_user, order_id, background_tasks, side=SIDE_VENDOR
    )
    return VendorSubscriptionCheckoutStatus(
        order=order,
        subscription=_build_status(db, current_user.uid),
    )


def apply_paid_order(db: Session, row: VendorWalletRecharge) -> bool:
    """Called by ``refresh_recharge`` in the same transaction that credited a
    newly PAID ``SUB:`` order: debit what was paid and add the period.

    Runs in a savepoint; on any failure the credit stays in the wallet (the
    vendor can pay from the balance later) and False is returned."""
    purpose = str(row.purpose or "")
    if not purpose.startswith(PURPOSE_PREFIX):
        return False
    plan = _find_plan(db, purpose[len(PURPOSE_PREFIX):])
    if plan is None:
        logger.error("SUBSCRIPTION_PLAN_MISSING order=%s", row.order_id)
        return False
    try:
        with db.begin_nested():
            _charge_wallet_and_add_period(
                db,
                user_uid=int(row.user_uid),
                plan=plan,
                amount_paise=int(row.amount_paise),
                method=METHOD_CASHFREE,
                idempotency_key=f"sub-cf:{row.order_id}",
                recharge_order_id=row.order_id,
            )
    except (InsufficientWalletBalance, SQLAlchemyError) as exc:
        logger.error(
            "SUBSCRIPTION_APPLY_FAILED order=%s category=%s", row.order_id, type(exc).__name__
        )
        return False
    logger.info("SUBSCRIPTION_PAID order=%s", row.order_id)
    return True


def set_auto_renew(
    db: Session,
    current_user: AuthenticatedUser,
    enabled: bool,
) -> VendorSubscriptionStatus:
    try:
        _load_eligible_vendor(db, current_user)
        state = _lock_or_create_state(db, current_user.uid)
        state.auto_renew = bool(enabled)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("auto_renew") from None
    return _build_status(db, current_user.uid)


def _period_item(row: VendorSubscriptionPeriod) -> VendorSubscriptionPeriodItem:
    return VendorSubscriptionPeriodItem(
        planCode=row.plan_code,
        amountPaise=int(row.amount_paise or 0),
        periodStart=_iso(row.period_start) or "",
        periodEnd=_iso(row.period_end) or "",
        paymentMethod=row.payment_method,
        transactionId=row.wallet_txn_ref,
        createdAt=_iso(row.created_at) or "",
    )


def list_history(db: Session, current_user: AuthenticatedUser) -> VendorSubscriptionHistory:
    try:
        _load_eligible_vendor(db, current_user)
        rows = (
            db.query(VendorSubscriptionPeriod)
            .filter(VendorSubscriptionPeriod.user_uid == current_user.uid)
            .order_by(VendorSubscriptionPeriod.id.desc())
            .limit(HISTORY_LIMIT)
            .all()
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("history") from None
    return VendorSubscriptionHistory(items=[_period_item(r) for r in rows])


# --- Push ----------------------------------------------------------------------


def notify_subscription(user_uid: int, title: str, body: str) -> None:
    """FCM push (``type=subscription``). Best effort, own DB session."""
    from ..database import SessionLocal
    from ..services.notifications import send_notification_to_user
    from ..utils.common import FCMSend

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.UID == int(user_uid)).first()
        user_app_id = str(getattr(user, "userAppId", "") or "").strip() if user else ""
        if not user_app_id:
            return
        result = send_notification_to_user(
            db,
            FCMSend(
                userAppId=user_app_id,
                title=title,
                body=body,
                url=SUBSCRIPTION_NOTIFICATION_URL,
                type=SUBSCRIPTION_NOTIFICATION_TYPE,
                soundFile="normal_notification",
            ),
        )
        message = getattr(result, "message", None)
        if message not in ("NOTIFICATION_SENT", "NO_TOKEN"):
            logger.warning("SUBSCRIPTION_PUSH_NOT_SENT uid=%s result=%s", user_uid, message)
    except Exception as exc:
        logger.warning("SUBSCRIPTION_PUSH_FAILED uid=%s category=%s", user_uid, type(exc).__name__)
    finally:
        db.close()


def queue_activated_push(
    background_tasks: Optional[BackgroundTasks],
    db: Session,
    user_uid: int,
    *,
    renewed: bool = False,
) -> None:
    try:
        state = _get_state(db, user_uid)
    except SQLAlchemyError:
        db.rollback()
        return
    if state is None or state.current_period_end is None:
        return
    until = _date_label(state.current_period_end)
    if renewed:
        title = "Subscription renewed"
        body = f"Your OpenBid vendor subscription was renewed from your wallet. It is active until {until}."
    else:
        title = "Subscription active"
        body = f"Your OpenBid vendor subscription is active until {until}. You can place bids now."
    wallet_notifications.dispatch(background_tasks, notify_subscription, int(user_uid), title, body)


# --- Internal: plans and grants (wallet ops key) --------------------------------


def _ops_plan_item(plan: VendorSubscriptionPlan) -> OpsSubscriptionPlanItem:
    return OpsSubscriptionPlanItem(
        code=plan.code,
        name=plan.name,
        pricePaise=int(plan.price_paise),
        durationDays=int(plan.duration_days),
        active=bool(plan.active),
    )


def ops_list_plans(db: Session) -> OpsSubscriptionPlanList:
    rows = db.query(VendorSubscriptionPlan).order_by(VendorSubscriptionPlan.id).all()
    return OpsSubscriptionPlanList(items=[_ops_plan_item(r) for r in rows])


def ops_upsert_plan(db: Session, code: str, payload: OpsSubscriptionPlanUpdate) -> OpsSubscriptionPlanItem:
    plan = _find_plan(db, code)
    if plan is None:
        if payload.name is None or payload.pricePaise is None or payload.durationDays is None:
            raise HTTPException(status_code=422, detail="PLAN_FIELDS_REQUIRED")
        plan = VendorSubscriptionPlan(code=code, active=True)
        db.add(plan)
    if payload.name is not None:
        plan.name = payload.name.strip()
    if payload.pricePaise is not None:
        plan.price_paise = int(payload.pricePaise)
    if payload.durationDays is not None:
        plan.duration_days = int(payload.durationDays)
    if payload.active is not None:
        plan.active = bool(payload.active)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("plan_upsert") from None
    logger.info(
        "SUBSCRIPTION_PLAN_UPDATED code=%s price_paise=%s days=%s active=%s",
        plan.code, plan.price_paise, plan.duration_days, plan.active,
    )
    return _ops_plan_item(plan)


def ops_grant(
    db: Session,
    payload: OpsSubscriptionGrantRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> OpsSubscriptionGrantResponse:
    """Give a vendor free days (support, referral). No wallet movement."""
    user = db.query(User).filter(User.userAppId == payload.userAppId).first()
    if user is None or not bool(user.alsoVendor):
        raise HTTPException(status_code=404, detail="VENDOR_NOT_FOUND")
    try:
        _period, created = add_period(
            db,
            user_uid=int(user.UID),
            plan_code="GRANT",
            days=payload.days,
            method=METHOD_OPS_GRANT,
            idempotency_key=f"sub-grant:{user.UID}:{payload.idempotencyKey}",
            note=payload.reason.strip(),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        created = False
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("grant") from None
    if created:
        logger.info("SUBSCRIPTION_GRANTED uid=%s days=%s", user.UID, payload.days)
        queue_activated_push(background_tasks, db, int(user.UID))
    state = _get_state(db, int(user.UID))
    return OpsSubscriptionGrantResponse(
        userAppId=payload.userAppId,
        currentPeriodEnd=_iso(state.current_period_end) or "",
        applied=created,
    )
