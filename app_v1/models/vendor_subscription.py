from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    func,
)

from ..database import Base

_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class VendorSubscriptionPlan(Base):
    """Purchasable vendor subscription plan (PR63).

    The only source of price and length: change ``price_paise`` here (SQL,
    the internal plans endpoint, or a future admin panel) and new purchases
    and renewals use it at once. There is no price in code; with no active
    row, purchases fail with ``SUBSCRIPTION_PLAN_NOT_CONFIGURED``.
    """

    __tablename__ = "vendor_subscription_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False, unique=True)
    name = Column(String(60), nullable=False)
    price_paise = Column(BigInteger, nullable=False)
    duration_days = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorSubscriptionPeriod(Base):
    """One paid (or granted) subscription period (PR63). Append-only.

    ``idempotency_key`` makes each purchase, renewal or grant apply once.
    """

    __tablename__ = "vendor_subscription_periods"

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    user_uid = Column(BigInteger, nullable=False, index=True)
    plan_code = Column(String(16), nullable=False)
    amount_paise = Column(BigInteger, nullable=False, default=0)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    # WALLET / CASHFREE / AUTO_RENEW / OPS_GRANT
    payment_method = Column(String(12), nullable=False)
    wallet_txn_ref = Column(String(40), nullable=True)
    recharge_order_id = Column(String(40), nullable=True)
    idempotency_key = Column(String(100), nullable=False, unique=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False)


class VendorSubscriptionState(Base):
    """Current subscription state, one row per vendor (PR63).

    Created on the first purchase or auto-renew toggle. Locked FOR UPDATE
    while a period is added so concurrent purchases extend sequentially.
    """

    __tablename__ = "vendor_subscription_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uid = Column(BigInteger, nullable=False, unique=True, index=True)
    current_period_end = Column(DateTime, nullable=True)
    auto_renew = Column(Boolean, nullable=False, default=True)
    # REMIND_3D / REMIND_1D / EXPIRED / RENEW_FAILED, for the period ending at
    # ``last_reminder_period_end`` (so a new period resets the reminders).
    last_reminder_kind = Column(String(20), nullable=True)
    last_reminder_period_end = Column(DateTime, nullable=True)
    last_reminder_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
