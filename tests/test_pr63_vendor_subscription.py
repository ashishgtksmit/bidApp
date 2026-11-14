"""
PR63 vendor subscription — DB-driven plan price, wallet purchase, Cashfree
checkout (recharge purpose), write gate, auto-renew / reminders, ops routes.

In-memory SQLite; Cashfree HTTP and FCM are mocked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.vendor_subscription import (  # noqa: E402
    VendorSubscriptionPeriod,
    VendorSubscriptionPlan,
    VendorSubscriptionState,
)
from app_v1.models.vendor_wallet import (  # noqa: E402
    VendorWallet,
    VendorWalletRecharge,
    VendorWalletTransaction,
)
from app_v1.endpoints.vendor_subscription import router as subscription_router  # noqa: E402
from app_v1.endpoints.vendor_wallet import router as wallet_router  # noqa: E402
from app_v1.crud import vendor_subscription as sub_crud  # noqa: E402
from app_v1.crud import vendor_subscription_renewal as renewal  # noqa: E402
from app_v1.crud import car_manage, driver_manage  # noqa: E402
from app_v1.crud.vendor_bid import require_subscribed_active_vendor  # noqa: E402
from app_v1.services import cashfree_pg  # noqa: E402
from app_v1.services import vendor_wallet_ledger as ledger  # noqa: E402

VENDOR = "8637554388"
VENDOR_UID = 1
SECRET = "unit-test-cashfree-secret"
OPS_KEY = "unit-test-wallet-ops-key"
INTERNAL_KEY = "unit-test-internal-key"

TABLES = [
    User.__table__,
    ApiRateLimitBucket.__table__,
    VendorWallet.__table__,
    VendorWalletTransaction.__table__,
    VendorWalletRecharge.__table__,
    VendorSubscriptionPlan.__table__,
    VendorSubscriptionPeriod.__table__,
    VendorSubscriptionState.__table__,
]


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    from app_v1.services import wallet_notifications

    sent = []
    monkeypatch.setattr(wallet_notifications, "notify_vendor_wallet", lambda *a: sent.append(("wallet",) + a))
    monkeypatch.setattr(wallet_notifications, "alert_ops_recharge_needs_review", lambda *a: None)
    monkeypatch.setattr(sub_crud, "notify_subscription", lambda *a: sent.append(("sub",) + a))
    return sent


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VENDOR_SUBSCRIPTION_ENABLED", "1")
    monkeypatch.setenv("VENDOR_SUBSCRIPTION_ENFORCED", "1")
    monkeypatch.setenv("WALLET_RECHARGE_ENABLED", "1")
    monkeypatch.setenv("CASHFREE_ENV", "SANDBOX")
    monkeypatch.setenv("CASHFREE_CLIENT_ID", "unit-test-client")
    monkeypatch.setenv("CASHFREE_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("CASHFREE_WALLET_NOTIFY_URL", "https://api.example.com/webhooks/cashfree/wallet")
    monkeypatch.setenv("WALLET_OPS_KEY", OPS_KEY)
    monkeypatch.setenv("INTERNAL_NOTIFICATION_KEY", INTERNAL_KEY)


def _auth(user_app_id=VENDOR, uid=VENDOR_UID) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"s-{user_app_id}",
        user_app_id=user_app_id,
        account_session_id="sess",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )


def _add_user(db, user_app_id=VENDOR, uid=VENDOR_UID, **kw):
    db.add(
        User(
            UID=uid,
            userAppId=user_app_id,
            password="secret",
            alternateNumber="1000000000",
            fullName="Vendor Name",
            emailId=f"{user_app_id}@example.com",
            dob="1990-01-01",
            city="Gangtok",
            gender="Male",
            profilePicture="images/profilepic_male.png",
            alsoVendor=kw.get("alsoVendor", True),
            vendorApproved=kw.get("vendorApproved", True),
            lockApp=False,
            customerRating="4.5",
            totalCustomerReviews=0,
            rating="4.5",
            totalNoOfReviews=0,
            joiningDate=date(2024, 1, 15),
            user_login_status="LOGGEDOUT",
        )
    )
    db.commit()


def _add_plan(db, price=50_000, days=30, active=True, code="MONTHLY"):
    db.add(VendorSubscriptionPlan(code=code, name="Monthly", price_paise=price, duration_days=days, active=active))
    db.commit()


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    _add_user(db)
    try:
        yield db, Session
    finally:
        db.close()


def _client(Session, auth=_auth()):
    app = FastAPI()
    app.include_router(subscription_router)
    app.include_router(wallet_router)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    if auth is not None:
        app.dependency_overrides[get_current_user] = lambda: auth
    return TestClient(app)


def _fund(db, paise, key="seed"):
    ledger.apply_wallet_entry(
        db,
        user_uid=VENDOR_UID,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=ledger.TXN_TYPE_ADJUSTMENT,
        amount_paise=paise,
        idempotency_key=key,
    )
    db.commit()


def _balance(db):
    db.expire_all()
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == VENDOR_UID).first()
    return int(wallet.balance_paise) if wallet else 0


def _state(db):
    db.expire_all()
    return db.query(VendorSubscriptionState).filter(VendorSubscriptionState.user_uid == VENDOR_UID).first()


def _set_period_end(db, end, auto_renew=True):
    state = _state(db) or VendorSubscriptionState(user_uid=VENDOR_UID)
    state.current_period_end = end
    state.auto_renew = auto_renew
    db.add(state)
    db.commit()


def _buy(client, key="buy-00000001"):
    return client.post("/vendor/subscription/purchase", json={"idempotencyKey": key})


# --- Status / plan from the database ---------------------------------------


def test_status_without_plan_or_subscription(env):
    _, Session = env
    body = _client(Session).get("/vendor/subscription").json()
    assert body["enabled"] is True and body["enforced"] is True
    assert body["active"] is False and body["status"] == "NONE"
    assert body["plan"] is None
    assert body["autoRenew"] is True


def test_status_reads_price_from_db_and_follows_changes(env):
    db, Session = env
    _add_plan(db, price=50_000)
    client = _client(Session)
    assert client.get("/vendor/subscription").json()["plan"]["pricePaise"] == 50_000
    db.query(VendorSubscriptionPlan).update({VendorSubscriptionPlan.price_paise: 75_000})
    db.commit()
    assert client.get("/vendor/subscription").json()["plan"]["pricePaise"] == 75_000


def test_purchase_without_plan_503(env):
    db, Session = env
    _fund(db, 100_000)
    resp = _buy(_client(Session))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "SUBSCRIPTION_PLAN_NOT_CONFIGURED"
    assert _balance(db) == 100_000


def test_purchase_inactive_plan_503(env):
    db, Session = env
    _add_plan(db, active=False)
    _fund(db, 100_000)
    assert _buy(_client(Session)).status_code == 503


def test_purchase_disabled_503(env, monkeypatch):
    db, Session = env
    _add_plan(db)
    monkeypatch.setenv("VENDOR_SUBSCRIPTION_ENABLED", "0")
    resp = _buy(_client(Session))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "SUBSCRIPTION_DISABLED"


def test_unapproved_vendor_403(env):
    db, Session = env
    db.query(User).update({User.vendorApproved: False})
    db.commit()
    resp = _client(Session).get("/vendor/subscription")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


# --- Wallet purchase ------------------------------------------------------------


def test_purchase_from_wallet_debits_db_price_and_activates(env, pushes):
    db, Session = env
    _add_plan(db, price=60_000, days=30)
    _fund(db, 100_000)
    body = _buy(_client(Session)).json()
    assert body["active"] is True and body["status"] == "ACTIVE"
    assert body["daysLeft"] == 30
    assert body["walletAvailablePaise"] == 40_000
    assert _balance(db) == 40_000
    txn = db.query(VendorWalletTransaction).filter(VendorWalletTransaction.txn_type == "SUBSCRIPTION").one()
    assert txn.direction == "DEBIT" and txn.amount_paise == 60_000 and txn.source == "VENDOR"
    period = db.query(VendorSubscriptionPeriod).one()
    assert period.payment_method == "WALLET" and period.amount_paise == 60_000
    assert period.wallet_txn_ref == txn.txn_ref
    assert [p[0] for p in pushes] == ["sub"]


def test_purchase_same_key_charges_once(env):
    db, Session = env
    _add_plan(db)
    _fund(db, 200_000)
    client = _client(Session)
    _buy(client)
    _buy(client)
    assert _balance(db) == 150_000
    assert db.query(VendorSubscriptionPeriod).count() == 1


def test_purchase_insufficient_balance_422_nothing_changes(env):
    db, Session = env
    _add_plan(db)
    _fund(db, 49_999)
    resp = _buy(_client(Session))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "INSUFFICIENT_BALANCE"
    assert _balance(db) == 49_999
    assert db.query(VendorSubscriptionPeriod).count() == 0
    assert _state(db) is None


def test_early_renewal_extends_from_current_end(env):
    db, Session = env
    _add_plan(db, days=30)
    _fund(db, 200_000)
    client = _client(Session)
    _buy(client, "buy-00000001")
    first_end = _state(db).current_period_end
    _buy(client, "buy-00000002")
    assert _state(db).current_period_end == first_end + timedelta(days=30)
    periods = db.query(VendorSubscriptionPeriod).order_by(VendorSubscriptionPeriod.id).all()
    assert periods[1].period_start == first_end


def test_purchase_after_expiry_starts_now(env):
    db, Session = env
    _add_plan(db, days=30)
    _fund(db, 100_000)
    _set_period_end(db, sub_crud._ist_now_naive() - timedelta(days=10))
    _buy(_client(Session))
    end = _state(db).current_period_end
    assert end > sub_crud._ist_now_naive() + timedelta(days=29)


def test_auto_renew_toggle_and_history(env):
    db, Session = env
    _add_plan(db)
    _fund(db, 100_000)
    client = _client(Session)
    _buy(client)
    assert client.put("/vendor/subscription/auto-renew", json={"enabled": False}).json()["autoRenew"] is False
    assert _state(db).auto_renew is False
    items = client.get("/vendor/subscription/history").json()["items"]
    assert len(items) == 1 and items[0]["paymentMethod"] == "WALLET" and items[0]["amountPaise"] == 50_000


# --- Cashfree checkout --------------------------------------------------------------


def _order(order_id, amount, status="ACTIVE"):
    return {
        "order_id": order_id,
        "cf_order_id": 99,
        "order_amount": amount,
        "order_currency": "INR",
        "order_status": status,
        "payment_session_id": "session_abc",
    }


def _checkout(client, key="chk-00000001", amount_rupees=500.0):
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _order(kw["order_id"], amount_rupees)) as create:
        resp = client.post("/vendor/subscription/checkout", json={"idempotencyKey": key})
    return resp, create


def test_checkout_uses_db_price_and_tags_purpose(env):
    db, Session = env
    _add_plan(db, price=45_000)
    resp, create = _checkout(_client(Session), amount_rupees=450.0)
    assert resp.status_code == 200
    assert resp.json()["amountPaise"] == 45_000
    assert create.call_args.kwargs["amount_paise"] == 45_000
    row = db.query(VendorWalletRecharge).one()
    assert row.purpose == "SUB:MONTHLY"


def test_checkout_price_below_recharge_min_is_allowed(env):
    db, Session = env
    _add_plan(db, price=5_000)
    resp, _ = _checkout(_client(Session), amount_rupees=50.0)
    assert resp.status_code == 200


def test_checkout_paid_credits_then_debits_and_activates(env, pushes):
    db, Session = env
    _add_plan(db)
    client = _client(Session)
    order_id = _checkout(client)[0].json()["orderId"]
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        first = client.get(f"/vendor/subscription/checkout/{order_id}").json()
        second = client.get(f"/vendor/subscription/checkout/{order_id}").json()
    assert first["order"]["status"] == "PAID"
    assert first["subscription"]["active"] is True
    assert second["subscription"]["currentPeriodEnd"] == first["subscription"]["currentPeriodEnd"]
    assert _balance(db) == 0
    types_ = sorted(t.txn_type for t in db.query(VendorWalletTransaction).all())
    assert types_ == ["RECHARGE", "SUBSCRIPTION"]
    period = db.query(VendorSubscriptionPeriod).one()
    assert period.payment_method == "CASHFREE" and period.recharge_order_id == order_id
    assert [p[0] for p in pushes] == ["sub"]


def test_checkout_paid_uses_paid_amount_even_if_price_changed(env):
    db, Session = env
    _add_plan(db)
    client = _client(Session)
    order_id = _checkout(client)[0].json()["orderId"]
    db.query(VendorSubscriptionPlan).update({VendorSubscriptionPlan.price_paise: 90_000})
    db.commit()
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        client.get(f"/vendor/subscription/checkout/{order_id}")
    assert _balance(db) == 0
    assert db.query(VendorSubscriptionPeriod).one().amount_paise == 50_000


def _signed(body: dict, ts="1730886660"):
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), ts.encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {"x-webhook-timestamp": ts, "x-webhook-signature": sig, "content-type": "application/json"}


def test_webhook_paid_activates_subscription_once(env):
    db, Session = env
    _add_plan(db)
    client = _client(Session)
    order_id = _checkout(client)[0].json()["orderId"]
    raw, headers = _signed({"data": {"order": {"order_id": order_id}}})
    webhook = _client(Session, auth=None)
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        assert webhook.post("/webhooks/cashfree/wallet", content=raw, headers=headers).status_code == 200
        assert webhook.post("/webhooks/cashfree/wallet", content=raw, headers=headers).status_code == 200
    assert db.query(VendorSubscriptionPeriod).count() == 1
    assert _balance(db) == 0
    assert sub_crud.is_subscription_active(db, VENDOR_UID)


def test_plain_recharge_is_not_a_subscription(env):
    db, Session = env
    _add_plan(db)
    client = _client(Session)
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _order(kw["order_id"], 500.0)):
        order_id = client.post(
            "/vendor/wallet/recharges", json={"amountPaise": 50_000, "idempotencyKey": "rch-00000001"}
        ).json()["orderId"]
    assert client.get(f"/vendor/subscription/checkout/{order_id}").status_code == 404
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        client.get(f"/vendor/wallet/recharges/{order_id}")
    assert _balance(db) == 50_000
    assert db.query(VendorSubscriptionPeriod).count() == 0


def test_subscription_failure_after_credit_keeps_money_in_wallet(env, pushes):
    db, Session = env
    _add_plan(db)
    client = _client(Session)
    order_id = _checkout(client)[0].json()["orderId"]
    with patch.object(sub_crud, "add_period", side_effect=sub_crud.SQLAlchemyError("boom")), \
            patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        body = client.get(f"/vendor/subscription/checkout/{order_id}").json()
    assert body["order"]["status"] == "PAID"
    assert body["subscription"]["active"] is False
    assert _balance(db) == 50_000
    assert [p[0] for p in pushes] == ["wallet"]


# --- Write gate ------------------------------------------------------------------------


def test_gate_blocks_without_subscription_when_enforced(env):
    db, _ = env
    with pytest.raises(HTTPException) as exc:
        require_subscribed_active_vendor(db, VENDOR)
    assert exc.value.status_code == 403
    assert exc.value.detail == "SUBSCRIPTION_REQUIRED"


def test_gate_open_when_not_enforced(env, monkeypatch):
    db, _ = env
    monkeypatch.setenv("VENDOR_SUBSCRIPTION_ENFORCED", "0")
    assert require_subscribed_active_vendor(db, VENDOR).UID == VENDOR_UID


def test_gate_passes_with_active_subscription(env):
    db, _ = env
    _set_period_end(db, sub_crud._ist_now_naive() + timedelta(hours=1))
    assert require_subscribed_active_vendor(db, VENDOR).UID == VENDOR_UID


def test_gate_blocks_after_expiry(env):
    db, _ = env
    _set_period_end(db, sub_crud._ist_now_naive() - timedelta(minutes=1))
    with pytest.raises(HTTPException):
        require_subscribed_active_vendor(db, VENDOR)


def test_gate_keeps_vendor_check_first(env):
    db, _ = env
    db.query(User).update({User.vendorApproved: False})
    db.commit()
    with pytest.raises(HTTPException) as exc:
        require_subscribed_active_vendor(db, VENDOR)
    assert exc.value.detail == "Not authorized as an active vendor"


@pytest.mark.parametrize(
    "call",
    [
        lambda db: car_manage.insert_car_for_vendor(db, MagicMock(), VENDOR),
        lambda db: driver_manage.insert_driver_for_vendor(db, MagicMock(), VENDOR),
        lambda db: driver_manage.update_driver_for_vendor(db, MagicMock(), VENDOR),
    ],
)
def test_car_and_driver_writes_are_gated(env, call):
    db, _ = env
    with pytest.raises(HTTPException) as exc:
        call(db)
    assert exc.value.detail == "SUBSCRIPTION_REQUIRED"


def test_bid_insert_and_update_are_gated(env):
    from app_v1.crud import vendor_bid

    db, _ = env
    with pytest.raises(HTTPException) as exc:
        vendor_bid.insert_vendor_bid(db, MagicMock(), VENDOR)
    assert exc.value.detail == "SUBSCRIPTION_REQUIRED"
    with pytest.raises(HTTPException) as exc:
        vendor_bid.update_vendor_bid(db, 1, MagicMock(), VENDOR)
    assert exc.value.detail == "SUBSCRIPTION_REQUIRED"


# --- Daily run --------------------------------------------------------------------------


def _run(Session, key=INTERNAL_KEY):
    return _client(Session, auth=None).post(
        "/internal/subscriptions/run-daily", headers={"X-OpenBid-Internal-Key": key}
    )


def test_run_daily_requires_internal_key(env):
    _, Session = env
    assert _run(Session, key="wrong").status_code == 403


def test_run_daily_auto_renews_within_24h(env, pushes):
    db, Session = env
    _add_plan(db)
    _fund(db, 100_000)
    end = sub_crud._ist_now_naive() + timedelta(hours=5)
    _set_period_end(db, end)
    body = _run(Session).json()
    assert body["renewed"] == 1
    assert _state(db).current_period_end == end + timedelta(days=30)
    assert _balance(db) == 50_000
    assert db.query(VendorSubscriptionPeriod).one().payment_method == "AUTO_RENEW"
    # Second run the same day does nothing.
    assert _run(Session).json() == {"renewed": 0, "renewFailed": 0, "reminded": 0, "expiredNotified": 0}
    assert _balance(db) == 50_000
    assert pushes[0][2] == "Subscription renewed"


def test_run_daily_renew_fails_on_low_balance_and_notifies_once(env, pushes):
    db, Session = env
    _add_plan(db)
    _set_period_end(db, sub_crud._ist_now_naive() + timedelta(hours=5))
    first = _run(Session).json()
    second = _run(Session).json()
    assert first["renewFailed"] == 1 and first["reminded"] == 1
    assert second["renewFailed"] == 1 and second["reminded"] == 0
    assert len(pushes) == 1 and pushes[0][2] == "Auto-renew could not be completed"


def test_run_daily_no_renew_when_auto_renew_off(env, pushes):
    db, Session = env
    _add_plan(db)
    _fund(db, 100_000)
    _set_period_end(db, sub_crud._ist_now_naive() + timedelta(hours=5), auto_renew=False)
    body = _run(Session).json()
    assert body["renewed"] == 0 and body["reminded"] == 1
    assert _balance(db) == 100_000
    assert pushes[0][2] == "Subscription ends tomorrow"


def test_run_daily_reminders_3d_then_expired(env, pushes):
    db, Session = env
    _add_plan(db)
    _set_period_end(db, sub_crud._ist_now_naive() + timedelta(days=2, hours=12), auto_renew=False)
    assert _run(Session).json()["reminded"] == 1
    assert _run(Session).json()["reminded"] == 0
    assert pushes[-1][2] == "Subscription ends in 3 days"
    _set_period_end(db, sub_crud._ist_now_naive() - timedelta(hours=2), auto_renew=False)
    assert _run(Session).json()["expiredNotified"] == 1
    assert _run(Session).json()["expiredNotified"] == 0
    assert pushes[-1][2] == "Subscription expired"


def test_run_daily_disabled_does_nothing(env, monkeypatch):
    db, Session = env
    _add_plan(db)
    _fund(db, 100_000)
    _set_period_end(db, sub_crud._ist_now_naive() + timedelta(hours=5))
    monkeypatch.setenv("VENDOR_SUBSCRIPTION_ENABLED", "0")
    assert _run(Session).json()["renewed"] == 0
    assert _balance(db) == 100_000


# --- Ops: plans and grants ----------------------------------------------------------------


def _ops(Session):
    return _client(Session, auth=None)


def test_ops_plan_update_changes_price_for_vendors(env):
    db, Session = env
    _add_plan(db)
    ops = _ops(Session)
    headers = {"X-OpenBid-Wallet-Ops-Key": OPS_KEY}
    resp = ops.put("/internal/subscriptions/plans/MONTHLY", json={"pricePaise": 60_000}, headers=headers)
    assert resp.status_code == 200 and resp.json()["pricePaise"] == 60_000
    assert _client(Session).get("/vendor/subscription").json()["plan"]["pricePaise"] == 60_000
    items = ops.get("/internal/subscriptions/plans", headers=headers).json()["items"]
    assert items == [{"code": "MONTHLY", "name": "Monthly", "pricePaise": 60_000, "durationDays": 30, "active": True}]


def test_ops_plan_create_requires_all_fields(env):
    _, Session = env
    headers = {"X-OpenBid-Wallet-Ops-Key": OPS_KEY}
    ops = _ops(Session)
    assert ops.put("/internal/subscriptions/plans/MONTHLY", json={"pricePaise": 50_000}, headers=headers).status_code == 422
    resp = ops.put(
        "/internal/subscriptions/plans/MONTHLY",
        json={"name": "Monthly", "pricePaise": 50_000, "durationDays": 30},
        headers=headers,
    )
    assert resp.status_code == 200


def test_ops_routes_need_ops_key(env):
    _, Session = env
    ops = _ops(Session)
    assert ops.get("/internal/subscriptions/plans").status_code == 403
    assert ops.put(
        "/internal/subscriptions/plans/MONTHLY",
        json={"pricePaise": 1},
        headers={"X-OpenBid-Internal-Key": INTERNAL_KEY},
    ).status_code == 403


def test_ops_grant_adds_days_once_without_wallet(env):
    db, Session = env
    headers = {"X-OpenBid-Wallet-Ops-Key": OPS_KEY}
    payload = {"userAppId": VENDOR, "days": 7, "reason": "Referral reward", "idempotencyKey": "grant-0000001"}
    first = _ops(Session).post("/internal/subscriptions/grant", json=payload, headers=headers).json()
    second = _ops(Session).post("/internal/subscriptions/grant", json=payload, headers=headers).json()
    assert first["applied"] is True and second["applied"] is False
    assert first["currentPeriodEnd"] == second["currentPeriodEnd"]
    assert db.query(VendorSubscriptionPeriod).one().payment_method == "OPS_GRANT"
    assert db.query(VendorWalletTransaction).count() == 0
    assert sub_crud.is_subscription_active(db, VENDOR_UID)
