"""PR48 wallet recharge via Cashfree PG (customer or vendor side; one wallet).

The wallet is credited only by ``refresh_recharge``, after a server-side
Cashfree order fetch whose order id, currency and amount match the stored
row. The app's SDK callback and the webhook body are only triggers.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser
from ..crud.vendor_wallet import SIDE_VENDOR, load_wallet_owner
from ..models.user_table import User
from ..models.vendor_wallet import VendorWalletRecharge
from ..schemas.vendor_wallet import WalletRechargeCreateRequest, WalletRechargeOrder
from ..services import cashfree_pg, wallet_notifications
from ..services.vendor_wallet_ledger import (
    DIRECTION_CREDIT,
    TXN_TYPE_RECHARGE,
    apply_wallet_entry,
)
from ..utils.rate_limit import enforce_rate_limit

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
ORDER_ID_PATTERN = re.compile(r"^OBW[0-9a-f]{24}$")
ORDER_TTL_MINUTES = 20

STATUS_CREATE_PENDING = "CREATE_PENDING"
STATUS_CREATE_FAILED = "CREATE_FAILED"
STATUS_PENDING = "PENDING"
STATUS_PAID = "PAID"
STATUS_FAILED = "FAILED"
STATUS_EXPIRED = "EXPIRED"
REFRESHABLE = frozenset({STATUS_CREATE_PENDING, STATUS_PENDING, STATUS_FAILED, STATUS_EXPIRED})


def recharge_min_paise() -> int:
    return int(os.getenv("WALLET_RECHARGE_MIN_PAISE", "10000"))


def recharge_max_paise() -> int:
    return int(os.getenv("WALLET_RECHARGE_MAX_PAISE", "5000000"))


def _ist_now_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _new_order_id() -> str:
    return f"OBW{secrets.token_hex(12)}"


def _require_enabled_config() -> cashfree_pg.CashfreeConfig:
    if (os.getenv("WALLET_RECHARGE_ENABLED") or "").strip() != "1":
        raise HTTPException(status_code=503, detail="WALLET_RECHARGE_DISABLED")
    try:
        return cashfree_pg.load_cashfree_config()
    except cashfree_pg.CashfreeNotConfigured:
        logger.error("WALLET_GATEWAY_NOT_CONFIGURED")
        raise HTTPException(status_code=503, detail="WALLET_GATEWAY_NOT_CONFIGURED") from None


def _customer_phone(user: User) -> Optional[str]:
    digits = re.sub(r"\D", "", str(getattr(user, "userAppId", "") or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def _customer_email(user: User) -> Optional[str]:
    email = str(getattr(user, "emailId", "") or "").strip()
    return email if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) else None


def _to_response(row: VendorWalletRecharge) -> WalletRechargeOrder:
    return WalletRechargeOrder(
        orderId=row.order_id,
        status=row.status,
        amountPaise=int(row.amount_paise),
        environment=row.environment,
        paymentSessionId=row.payment_session_id if row.status == STATUS_PENDING else None,
        creditedTransactionId=row.credited_txn_ref,
        source=row.source,
    )


def _db_error(op: str) -> HTTPException:
    logger.error("WALLET_RECHARGE_DB_FAILED op=%s", op)
    return HTTPException(status_code=500, detail="WALLET_QUERY_FAILED")


def _apply_order_session(row: VendorWalletRecharge, order: dict) -> None:
    session_id = str(order.get("payment_session_id") or "").strip()
    if session_id:
        row.payment_session_id = session_id
    cf_order_id = order.get("cf_order_id")
    if cf_order_id is not None:
        row.cf_order_id = str(cf_order_id)


def _open_gateway_order(
    db: Session,
    config: cashfree_pg.CashfreeConfig,
    row: VendorWalletRecharge,
    user: User,
    *,
    recover: bool,
) -> None:
    """Create the Cashfree order, or with ``recover`` first look for one
    already created by an earlier attempt whose response was lost."""
    if recover:
        try:
            existing = cashfree_pg.get_order(config, row.order_id)
        except cashfree_pg.CashfreeError as exc:
            if not (exc.definite and exc.status_code == 404):
                raise HTTPException(status_code=503, detail="WALLET_GATEWAY_UNAVAILABLE") from None
        else:
            _apply_order_session(row, existing)
            if row.payment_session_id:
                row.status = STATUS_PENDING
                row.failure_code = None
                db.commit()
                return

    phone = _customer_phone(user)
    if phone is None:
        row.status = STATUS_CREATE_FAILED
        row.failure_code = "PROFILE_PHONE_INVALID"
        db.commit()
        raise HTTPException(status_code=422, detail="WALLET_PROFILE_INCOMPLETE")

    try:
        order = cashfree_pg.create_order(
            config,
            order_id=row.order_id,
            amount_paise=int(row.amount_paise),
            customer_id=f"obv{row.user_uid}",
            customer_phone=phone,
            customer_name=str(getattr(user, "fullName", "") or "").strip() or None,
            customer_email=_customer_email(user),
            expires_at_ist=_ist_now_naive() + timedelta(minutes=ORDER_TTL_MINUTES),
        )
    except cashfree_pg.CashfreeError as exc:
        if exc.definite:
            row.status = STATUS_CREATE_FAILED
            row.failure_code = f"GATEWAY_HTTP_{exc.status_code}"
            db.commit()
            raise HTTPException(status_code=502, detail="WALLET_GATEWAY_ERROR") from None
        row.failure_code = "GATEWAY_UNAVAILABLE"
        db.commit()
        raise HTTPException(status_code=503, detail="WALLET_GATEWAY_UNAVAILABLE") from None

    _apply_order_session(row, order)
    if not row.payment_session_id:
        row.failure_code = "GATEWAY_NO_SESSION"
        db.commit()
        raise HTTPException(status_code=503, detail="WALLET_GATEWAY_UNAVAILABLE")
    row.status = STATUS_PENDING
    row.failure_code = None
    db.commit()


def create_recharge(
    db: Session,
    current_user: AuthenticatedUser,
    payload: WalletRechargeCreateRequest,
    *,
    side: str = SIDE_VENDOR,
    purpose: Optional[str] = None,
) -> WalletRechargeOrder:
    """``purpose`` (PR63) tags the order, e.g. ``SUB:MONTHLY``; the amount
    is then the server-side plan price, so recharge min/max do not apply."""
    config = _require_enabled_config()
    amount = payload.amountPaise
    if purpose is None:
        if amount < recharge_min_paise():
            raise HTTPException(status_code=422, detail="RECHARGE_BELOW_MIN")
        if amount > recharge_max_paise():
            raise HTTPException(status_code=422, detail="RECHARGE_ABOVE_MAX")

    try:
        user = load_wallet_owner(db, current_user, side)
        existing = (
            db.query(VendorWalletRecharge)
            .filter(
                VendorWalletRecharge.user_uid == current_user.uid,
                VendorWalletRecharge.client_idempotency_key == payload.idempotencyKey,
            )
            .first()
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("create_lookup") from None

    if existing is not None:
        if int(existing.amount_paise) != amount or existing.purpose != purpose:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_REUSED")
        if existing.status == STATUS_CREATE_PENDING:
            _open_gateway_order(db, config, existing, user, recover=True)
        return _to_response(existing)

    limited = enforce_rate_limit(
        db,
        bucket_key=f"wallet_recharge:uid:{current_user.uid}",
        max_hits=int(os.getenv("RATE_LIMIT_WALLET_RECHARGE_PER_USER", "10")),
        window_seconds=int(os.getenv("RATE_LIMIT_WALLET_RECHARGE_WINDOW_SECONDS", "900")),
    )
    if limited is not None:
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    row = VendorWalletRecharge(
        order_id=_new_order_id(),
        user_uid=current_user.uid,
        amount_paise=amount,
        currency="INR",
        status=STATUS_CREATE_PENDING,
        environment=config.environment,
        client_idempotency_key=payload.idempotencyKey,
        source=side,
        purpose=purpose,
        created_at=_ist_now_naive(),
    )
    try:
        db.add(row)
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = (
            db.query(VendorWalletRecharge)
            .filter(
                VendorWalletRecharge.user_uid == current_user.uid,
                VendorWalletRecharge.client_idempotency_key == payload.idempotencyKey,
            )
            .first()
        )
        if raced is None:
            raise _db_error("create_insert") from None
        return _to_response(raced)
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("create_insert") from None

    _open_gateway_order(db, config, row, user, recover=False)
    return _to_response(row)


def _interpret_and_apply(db: Session, row: VendorWalletRecharge, order: dict) -> None:
    """Apply a fetched Cashfree order to a locked recharge row. Caller commits."""
    if str(order.get("order_id") or "") != row.order_id:
        row.failure_code = "ORDER_MISMATCH"
        logger.error("WALLET_RECHARGE_MISMATCH kind=order order=%s", row.order_id)
        return
    if str(order.get("order_currency") or "").upper() != "INR":
        row.failure_code = "CURRENCY_MISMATCH"
        logger.error("WALLET_RECHARGE_MISMATCH kind=currency order=%s", row.order_id)
        return
    if cashfree_pg.amount_to_paise(order.get("order_amount")) != int(row.amount_paise):
        row.failure_code = "AMOUNT_MISMATCH"
        logger.error("WALLET_RECHARGE_MISMATCH kind=amount order=%s", row.order_id)
        return

    _apply_order_session(row, order)
    order_status = str(order.get("order_status") or "").upper()
    if order_status == "PAID":
        txn = apply_wallet_entry(
            db,
            user_uid=int(row.user_uid),
            direction=DIRECTION_CREDIT,
            txn_type=TXN_TYPE_RECHARGE,
            amount_paise=int(row.amount_paise),
            idempotency_key=f"cf:{row.order_id}",
            description="Wallet recharge",
            reference_id=row.order_id,
            source=row.source,
        )
        row.status = STATUS_PAID
        row.failure_code = None
        row.credited_txn_ref = txn.txn_ref
        row.paid_at = row.paid_at or _ist_now_naive()
        logger.info("WALLET_RECHARGE_PAID order=%s", row.order_id)
    elif order_status == "ACTIVE":
        if row.payment_session_id:
            row.status = STATUS_PENDING
    elif order_status == "EXPIRED":
        row.status = STATUS_EXPIRED
    elif order_status == "TERMINATED":
        row.status = STATUS_FAILED
    elif order_status != "TERMINATION_REQUESTED":
        row.failure_code = "RECONCILIATION_REQUIRED"
        logger.warning("WALLET_RECHARGE_UNKNOWN_STATUS order=%s", row.order_id)


REVIEW_FAILURE_CODES = frozenset(
    {"ORDER_MISMATCH", "CURRENCY_MISMATCH", "AMOUNT_MISMATCH", "RECONCILIATION_REQUIRED"}
)


def _queue_side_effects(
    background_tasks: Optional[BackgroundTasks],
    row: VendorWalletRecharge,
    *,
    newly_paid: bool,
    alert: bool,
) -> None:
    if newly_paid:
        wallet_notifications.dispatch(
            background_tasks,
            wallet_notifications.notify_vendor_wallet,
            int(row.user_uid),
            "Wallet recharged",
            f"{wallet_notifications.rupees(int(row.amount_paise))} has been added to your OpenBid wallet.",
        )
    if alert:
        wallet_notifications.dispatch(
            background_tasks,
            wallet_notifications.alert_ops_recharge_needs_review,
            row.order_id,
            int(row.user_uid),
            int(row.amount_paise),
            str(row.failure_code),
            row.environment,
        )


def refresh_recharge(
    db: Session,
    order_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
) -> Optional[VendorWalletRecharge]:
    """Re-fetch the order from Cashfree and apply it.

    Returns the row (refreshed or unchanged), or None if unknown.
    Raises ``cashfree_pg.CashfreeError`` when the gateway answer is ambiguous.
    Queues the vendor push on first credit and one ops alert per order when
    it needs manual review.
    """
    row = (
        db.query(VendorWalletRecharge)
        .filter(VendorWalletRecharge.order_id == order_id)
        .first()
    )
    if row is None:
        return None
    if row.status not in REFRESHABLE:
        return row
    try:
        config = cashfree_pg.load_cashfree_config()
    except cashfree_pg.CashfreeNotConfigured:
        return row
    if config.environment != row.environment:
        return row

    try:
        order = cashfree_pg.get_order(config, row.order_id)
    except cashfree_pg.CashfreeError as exc:
        if exc.definite and exc.status_code == 404:
            locked = (
                db.query(VendorWalletRecharge)
                .filter(VendorWalletRecharge.id == row.id)
                .with_for_update()
                .first()
            )
            if locked.status == STATUS_CREATE_PENDING:
                locked.status = STATUS_CREATE_FAILED
                locked.failure_code = "GATEWAY_ORDER_NOT_FOUND"
            db.commit()
            return locked
        raise

    locked = (
        db.query(VendorWalletRecharge)
        .filter(VendorWalletRecharge.id == row.id)
        .with_for_update()
        .first()
    )
    newly_paid = alert = subscription_applied = False
    if locked.status in REFRESHABLE:
        _interpret_and_apply(db, locked, order)
        newly_paid = locked.status == STATUS_PAID
        alert = locked.failure_code in REVIEW_FAILURE_CODES and locked.ops_alerted_at is None
        if alert:
            locked.ops_alerted_at = _ist_now_naive()
        if newly_paid and locked.purpose:
            from . import vendor_subscription

            subscription_applied = vendor_subscription.apply_paid_order(db, locked)
    db.commit()
    if subscription_applied:
        from . import vendor_subscription

        vendor_subscription.queue_activated_push(background_tasks, db, int(locked.user_uid))
        newly_paid = False
    _queue_side_effects(background_tasks, locked, newly_paid=newly_paid, alert=alert)
    return locked


def get_recharge_status(
    db: Session,
    current_user: AuthenticatedUser,
    order_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
    *,
    side: str = SIDE_VENDOR,
) -> WalletRechargeOrder:
    if not ORDER_ID_PATTERN.match(order_id or ""):
        raise HTTPException(status_code=404, detail="RECHARGE_NOT_FOUND")
    try:
        load_wallet_owner(db, current_user, side)
        row = (
            db.query(VendorWalletRecharge)
            .filter(
                VendorWalletRecharge.order_id == order_id,
                VendorWalletRecharge.user_uid == current_user.uid,
            )
            .first()
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db_error("status_lookup") from None
    if row is None:
        raise HTTPException(status_code=404, detail="RECHARGE_NOT_FOUND")

    if row.status in (STATUS_CREATE_PENDING, STATUS_PENDING):
        try:
            row = refresh_recharge(db, order_id, background_tasks) or row
        except cashfree_pg.CashfreeError:
            db.rollback()
        except SQLAlchemyError:
            db.rollback()
            raise _db_error("status_refresh") from None
    return _to_response(row)


def handle_cashfree_webhook(
    db: Session,
    *,
    raw_body: bytes,
    timestamp: Optional[str],
    signature: Optional[str],
    payload: Optional[dict],
    background_tasks: Optional[BackgroundTasks] = None,
) -> int:
    """Returns the HTTP status for Cashfree: 200 done/ignored, 401 bad
    signature, 503 retry later."""
    try:
        config = cashfree_pg.load_cashfree_config()
    except cashfree_pg.CashfreeNotConfigured:
        logger.error("WALLET_WEBHOOK_NOT_CONFIGURED")
        return 503
    if not cashfree_pg.verify_webhook_signature(
        raw_body=raw_body,
        timestamp=timestamp,
        signature=signature,
        secret=config.client_secret,
    ):
        logger.warning("WALLET_WEBHOOK_BAD_SIGNATURE")
        return 401

    order_id = ""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("order"), dict):
            order_id = str(data["order"].get("order_id") or "")
    if not ORDER_ID_PATTERN.match(order_id):
        return 200

    try:
        refresh_recharge(db, order_id, background_tasks)
    except cashfree_pg.CashfreeError:
        db.rollback()
        return 503
    except SQLAlchemyError:
        db.rollback()
        logger.error("WALLET_WEBHOOK_DB_FAILED order=%s", order_id)
        return 503
    return 200
