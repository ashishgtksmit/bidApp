"""PR63 daily subscription job: wallet auto-renew and expiry reminders.

Called by the worker timer through ``POST /internal/subscriptions/run-daily``.
Idempotent: a renewal is keyed by the period end it extends, and each
reminder kind is sent at most once per period end, so running it more than
once a day is harmless.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.user_table import User
from ..models.vendor_subscription import VendorSubscriptionPlan, VendorSubscriptionState
from ..schemas.vendor_subscription import SubscriptionDailyRunResult
from ..services import wallet_notifications
from ..services.vendor_wallet_ledger import InsufficientWalletBalance
from . import vendor_subscription as sub

logger = logging.getLogger(__name__)

RENEW_AHEAD = timedelta(hours=24)
# Missed runs / late top-ups: still auto-renew shortly after expiry.
RENEW_GRACE = timedelta(days=2)
REMIND_3D_AHEAD = timedelta(days=3)
REMIND_1D_AHEAD = timedelta(days=1)
EXPIRED_NOTICE_WINDOW = timedelta(days=2)

KIND_REMIND_3D = "REMIND_3D"
KIND_REMIND_1D = "REMIND_1D"
KIND_RENEW_FAILED = "RENEW_FAILED"
KIND_EXPIRED = "EXPIRED"
_KIND_RANK = {KIND_REMIND_3D: 1, KIND_REMIND_1D: 2, KIND_RENEW_FAILED: 2, KIND_EXPIRED: 3}


def _eligible_vendor(db: Session, user_uid: int) -> bool:
    user = db.query(User).filter(User.UID == int(user_uid)).first()
    return bool(
        user is not None
        and bool(user.alsoVendor)
        and bool(user.vendorApproved)
        and not bool(user.lockApp)
    )


def _already_sent(state: VendorSubscriptionState, kind: str) -> bool:
    if state.last_reminder_period_end != state.current_period_end:
        return False
    return _KIND_RANK.get(state.last_reminder_kind or "", 0) >= _KIND_RANK[kind]


def _mark_sent(state: VendorSubscriptionState, kind: str) -> None:
    state.last_reminder_kind = kind
    state.last_reminder_period_end = state.current_period_end
    state.last_reminder_at = sub._ist_now_naive()


def _message(kind: str, state: VendorSubscriptionState, plan: Optional[VendorSubscriptionPlan]) -> tuple[str, str]:
    end_label = sub._date_label(state.current_period_end)
    price = wallet_notifications.rupees(int(plan.price_paise)) if plan is not None else None
    if kind == KIND_EXPIRED:
        return (
            "Subscription expired",
            "Your OpenBid vendor subscription has expired. Renew it to place bids, add cars and add drivers.",
        )
    if kind == KIND_RENEW_FAILED:
        need = f"Add at least {price} to your wallet" if price else "Add money to your wallet"
        return (
            "Auto-renew could not be completed",
            f"Your subscription ends on {end_label} and your wallet balance is too low. {need} or renew now.",
        )
    when = "in 3 days" if kind == KIND_REMIND_3D else "tomorrow"
    if bool(state.auto_renew) and price:
        return (
            f"Subscription renews {when}",
            f"Your subscription will auto-renew on {end_label} for {price} from your wallet. Keep enough balance.",
        )
    return (
        f"Subscription ends {when}",
        f"Your OpenBid vendor subscription ends on {end_label}. Renew to keep placing bids.",
    )


def _send(state: VendorSubscriptionState, kind: str, plan: Optional[VendorSubscriptionPlan]) -> None:
    title, body = _message(kind, state, plan)
    wallet_notifications.dispatch(None, sub.notify_subscription, int(state.user_uid), title, body)


def _try_auto_renew(db: Session, state_id: int, plan: VendorSubscriptionPlan) -> Optional[bool]:
    """True renewed, False insufficient balance, None skipped / error.
    Commits or rolls back its own work."""
    state = (
        db.query(VendorSubscriptionState)
        .filter(VendorSubscriptionState.id == state_id)
        .with_for_update()
        .first()
    )
    now = sub._ist_now_naive()
    end = state.current_period_end if state is not None else None
    if state is None or not bool(state.auto_renew) or end is None:
        db.rollback()
        return None
    if not (now - RENEW_GRACE < end <= now + RENEW_AHEAD):
        db.rollback()
        return None
    key = f"sub-auto:{state.user_uid}:{end.strftime('%Y%m%d%H%M%S')}"
    try:
        sub._charge_wallet_and_add_period(
            db,
            user_uid=int(state.user_uid),
            plan=plan,
            amount_paise=int(plan.price_paise),
            method=sub.METHOD_AUTO_RENEW,
            idempotency_key=key,
        )
        db.commit()
        return True
    except InsufficientWalletBalance:
        db.rollback()
        return False
    except IntegrityError:
        db.rollback()
        return None


def run_daily(db: Session) -> SubscriptionDailyRunResult:
    renewed = renew_failed = reminded = expired_notified = 0
    if not sub.subscription_enabled():
        return SubscriptionDailyRunResult(renewed=0, renewFailed=0, reminded=0, expiredNotified=0)

    plan = sub._find_plan(db, sub.DEFAULT_PLAN_CODE)
    renew_plan = plan if plan is not None and bool(plan.active) and int(plan.price_paise or 0) > 0 else None
    if renew_plan is None:
        logger.error("SUBSCRIPTION_RUN_NO_PLAN auto-renew skipped")

    now = sub._ist_now_naive()
    state_ids = [
        row.id
        for row in db.query(VendorSubscriptionState.id)
        .filter(
            VendorSubscriptionState.current_period_end.isnot(None),
            VendorSubscriptionState.current_period_end > now - EXPIRED_NOTICE_WINDOW,
            VendorSubscriptionState.current_period_end <= now + REMIND_3D_AHEAD,
        )
        .all()
    ]
    db.rollback()

    for state_id in state_ids:
        try:
            state = db.query(VendorSubscriptionState).filter(VendorSubscriptionState.id == state_id).first()
            if state is None or not _eligible_vendor(db, int(state.user_uid)):
                db.rollback()
                continue
            uid = int(state.user_uid)

            renew_result = None
            if renew_plan is not None:
                renew_result = _try_auto_renew(db, state_id, renew_plan)
            if renew_result is True:
                renewed += 1
                sub.queue_activated_push(None, db, uid, renewed=True)
                continue

            state = (
                db.query(VendorSubscriptionState)
                .filter(VendorSubscriptionState.id == state_id)
                .with_for_update()
                .first()
            )
            now = sub._ist_now_naive()
            end = state.current_period_end
            remaining = end - now
            if renew_result is False:
                renew_failed += 1
                kind = KIND_EXPIRED if remaining <= timedelta(0) else KIND_RENEW_FAILED
            elif remaining <= timedelta(0):
                kind = KIND_EXPIRED
            elif remaining <= REMIND_1D_AHEAD:
                kind = KIND_REMIND_1D
            else:
                kind = KIND_REMIND_3D

            if _already_sent(state, kind):
                db.rollback()
                continue
            _mark_sent(state, kind)
            db.commit()
            _send(state, kind, renew_plan)
            if kind == KIND_EXPIRED:
                expired_notified += 1
            else:
                reminded += 1
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("SUBSCRIPTION_RUN_FAILED state=%s category=%s", state_id, type(exc).__name__)

    logger.info(
        "SUBSCRIPTION_RUN_DONE renewed=%s renew_failed=%s reminded=%s expired=%s",
        renewed, renew_failed, reminded, expired_notified,
    )
    return SubscriptionDailyRunResult(
        renewed=renewed,
        renewFailed=renew_failed,
        reminded=reminded,
        expiredNotified=expired_notified,
    )
