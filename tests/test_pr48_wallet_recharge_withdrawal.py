"""
PR48 vendor wallet — Cashfree recharge, webhook, withdrawal (manual payout), ops.

In-memory SQLite; Cashfree HTTP, SMS and SMTP are mocked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
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
from app_v1.models.vendor_wallet import (  # noqa: E402
    VendorWallet,
    VendorWalletOtpChallenge,
    VendorWalletRecharge,
    VendorWalletTransaction,
    VendorWalletWithdrawal,
)
from app_v1.endpoints.vendor_wallet import router as wallet_router  # noqa: E402
from app_v1.services import cashfree_pg  # noqa: E402
from app_v1.services import vendor_wallet_ledger as ledger  # noqa: E402
from app_v1.crud import vendor_wallet_withdrawal as withdrawal_crud  # noqa: E402

VENDOR = "8637554388"
OTHER = "8637554399"
VENDOR_UID = 1
OTHER_UID = 2
SECRET = "unit-test-cashfree-secret"
OPS_KEY = "unit-test-wallet-ops-key"
FIXED_OTP = "246810"
ACCOUNT_NO = "123456789012"

TABLES = [
    User.__table__,
    ApiRateLimitBucket.__table__,
    VendorWallet.__table__,
    VendorWalletTransaction.__table__,
    VendorWalletRecharge.__table__,
    VendorWalletWithdrawal.__table__,
    VendorWalletOtpChallenge.__table__,
]


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    from app_v1.services import wallet_notifications

    monkeypatch.setattr(wallet_notifications, "notify_vendor_wallet", lambda *a: None)
    monkeypatch.setattr(wallet_notifications, "alert_ops_recharge_needs_review", lambda *a: None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WALLET_RECHARGE_ENABLED", "1")
    monkeypatch.setenv("WALLET_WITHDRAWAL_ENABLED", "1")
    monkeypatch.setenv("CASHFREE_ENV", "SANDBOX")
    monkeypatch.setenv("CASHFREE_CLIENT_ID", "unit-test-client")
    monkeypatch.setenv("CASHFREE_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("CASHFREE_WALLET_NOTIFY_URL", "https://api.example.com/webhooks/cashfree/wallet")
    monkeypatch.setenv("WALLET_OPS_KEY", OPS_KEY)
    monkeypatch.setenv("OTP_TEST_BYPASS_SMS", "1")
    monkeypatch.setenv("OTP_TEST_FIXED_OTP", FIXED_OTP)
    monkeypatch.delenv("WALLET_OPS_EMAIL", raising=False)


def _auth(user_app_id: str, uid: int) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"s-{user_app_id}",
        user_app_id=user_app_id,
        account_session_id="sess",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )


def _add_user(db, user_app_id, uid, **kw):
    db.add(
        User(
            UID=uid,
            userAppId=user_app_id,
            password="secret",
            alternateNumber="1000000000",
            fullName=kw.get("fullName", "Vendor Name"),
            emailId=f"{user_app_id}@example.com",
            dob="1990-01-01",
            city="Gangtok",
            gender="Male",
            profilePicture="images/profilepic_male.png",
            alsoVendor=True,
            vendorApproved=True,
            lockApp=False,
            customerRating="4.5",
            totalCustomerReviews=0,
            rating="4.5",
            totalNoOfReviews=0,
            joiningDate=date(2024, 1, 15),
            user_login_status="LOGGEDOUT",
            bankAccountHolderName=kw.get("holder", "Vendor Name"),
            bankAccountNo=kw.get("account", ACCOUNT_NO),
            bankIFSC=kw.get("ifsc", "SBIN0001234"),
            bankName=kw.get("bankName", "State Bank of India"),
        )
    )
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
    _add_user(db, VENDOR, VENDOR_UID)
    _add_user(db, OTHER, OTHER_UID)
    try:
        yield db, Session
    finally:
        db.close()


def _client(Session, auth=None):
    app = FastAPI()
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


def _vendor(Session):
    return _client(Session, _auth(VENDOR, VENDOR_UID))


def _wallet(db, uid=VENDOR_UID):
    db.expire_all()
    return db.query(VendorWallet).filter(VendorWallet.user_uid == uid).first()


def _fund(db, paise, uid=VENDOR_UID, key="seed"):
    ledger.apply_wallet_entry(
        db,
        user_uid=uid,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=ledger.TXN_TYPE_ADJUSTMENT,
        amount_paise=paise,
        idempotency_key=key,
    )
    db.commit()


def _order(order_id, amount, status="ACTIVE", currency="INR"):
    return {
        "order_id": order_id,
        "cf_order_id": 99,
        "order_amount": amount,
        "order_currency": currency,
        "order_status": status,
        "payment_session_id": "session_abc",
    }


def _create(client, amount=50_000, key="key-00000001"):
    return client.post(
        "/vendor/wallet/recharges",
        json={"amountPaise": amount, "idempotencyKey": key},
    )


# --- Recharge: create -------------------------------------------------------


def test_recharge_disabled(env, monkeypatch):
    _, Session = env
    monkeypatch.setenv("WALLET_RECHARGE_ENABLED", "0")
    resp = _create(_vendor(Session))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "WALLET_RECHARGE_DISABLED"


def test_recharge_not_configured(env, monkeypatch):
    _, Session = env
    monkeypatch.delenv("CASHFREE_CLIENT_SECRET")
    resp = _create(_vendor(Session))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "WALLET_GATEWAY_NOT_CONFIGURED"


@pytest.mark.parametrize("amount,detail", [(9_999, "RECHARGE_BELOW_MIN"), (5_000_001, "RECHARGE_ABOVE_MAX")])
def test_recharge_limits(env, amount, detail):
    _, Session = env
    resp = _create(_vendor(Session), amount=amount)
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail


def test_recharge_create_success_and_idempotent(env):
    db, Session = env
    client = _vendor(Session)
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _order(kw["order_id"], 500.0)) as create:
        first = _create(client)
        second = _create(client)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "PENDING"
    assert body["paymentSessionId"] == "session_abc"
    assert body["environment"] == "SANDBOX"
    assert body["orderId"].startswith("OBW")
    assert second.json()["orderId"] == body["orderId"]
    assert create.call_count == 1
    kwargs = create.call_args.kwargs
    assert kwargs["amount_paise"] == 50_000
    assert kwargs["customer_phone"] == VENDOR
    assert _wallet(db) is None


def test_recharge_key_reuse_with_other_amount_409(env):
    _, Session = env
    client = _vendor(Session)
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _order(kw["order_id"], 500.0)):
        _create(client)
        resp = _create(client, amount=60_000)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "IDEMPOTENCY_KEY_REUSED"


def test_recharge_gateway_rejects_definitely(env):
    db, Session = env
    with patch.object(cashfree_pg, "create_order", side_effect=cashfree_pg.CashfreeError("http", status_code=400, definite=True)):
        resp = _create(_vendor(Session))
    assert resp.status_code == 502
    db.expire_all()
    assert db.query(VendorWalletRecharge).one().status == "CREATE_FAILED"


def test_recharge_ambiguous_then_recovered_without_second_create(env):
    db, Session = env
    client = _vendor(Session)
    with patch.object(cashfree_pg, "create_order", side_effect=cashfree_pg.CashfreeError("timeout")):
        resp = _create(client)
    assert resp.status_code == 503
    row = db.query(VendorWalletRecharge).one()
    assert row.status == "CREATE_PENDING"
    with patch.object(cashfree_pg, "get_order", return_value=_order(row.order_id, 500.0)) as get, \
            patch.object(cashfree_pg, "create_order") as create:
        retry = _create(client)
    assert retry.status_code == 200
    assert retry.json()["status"] == "PENDING"
    assert get.call_count == 1
    create.assert_not_called()


def test_recharge_invalid_phone_422(env):
    db, Session = env
    db.query(User).filter(User.UID == VENDOR_UID).update({User.userAppId: "abc"})
    db.commit()
    resp = _create(_client(Session, _auth("abc", VENDOR_UID)))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "WALLET_PROFILE_INCOMPLETE"


# --- Recharge: status / credit ---------------------------------------------


def _pending_order(db, Session):
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _order(kw["order_id"], 500.0)):
        return _create(_vendor(Session)).json()["orderId"]


def test_status_paid_credits_once(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    client = _vendor(Session)
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        first = client.get(f"/vendor/wallet/recharges/{order_id}").json()
        second = client.get(f"/vendor/wallet/recharges/{order_id}").json()
    assert first["status"] == "PAID"
    assert first["creditedTransactionId"].startswith("WT")
    assert first["paymentSessionId"] is None
    assert second["creditedTransactionId"] == first["creditedTransactionId"]
    assert _wallet(db).balance_paise == 50_000
    assert db.query(VendorWalletTransaction).count() == 1


@pytest.mark.parametrize(
    "order_patch,code",
    [
        ({"order_amount": 499.99}, "AMOUNT_MISMATCH"),
        ({"order_amount": 5000.0}, "AMOUNT_MISMATCH"),
        ({"order_currency": "USD"}, "CURRENCY_MISMATCH"),
        ({"order_id": "OBW" + "0" * 24}, "ORDER_MISMATCH"),
    ],
)
def test_status_mismatch_never_credits(env, order_patch, code):
    db, Session = env
    order_id = _pending_order(db, Session)
    order = _order(order_id, 500.0, "PAID")
    order.update(order_patch)
    with patch.object(cashfree_pg, "get_order", return_value=order):
        body = _vendor(Session).get(f"/vendor/wallet/recharges/{order_id}").json()
    assert body["status"] == "PENDING"
    assert _wallet(db) is None
    db.expire_all()
    assert db.query(VendorWalletRecharge).one().failure_code == code


def test_status_expired(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "EXPIRED")):
        body = _vendor(Session).get(f"/vendor/wallet/recharges/{order_id}").json()
    assert body["status"] == "EXPIRED"


def test_status_gateway_down_returns_pending(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    with patch.object(cashfree_pg, "get_order", side_effect=cashfree_pg.CashfreeError("network")):
        resp = _vendor(Session).get(f"/vendor/wallet/recharges/{order_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "PENDING"


def test_status_other_users_order_404(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    resp = _client(Session, _auth(OTHER, OTHER_UID)).get(f"/vendor/wallet/recharges/{order_id}")
    assert resp.status_code == 404


# --- Webhook -----------------------------------------------------------------


def _signed(body: dict, secret=SECRET, ts="1730886660"):
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {"x-webhook-timestamp": ts, "x-webhook-signature": sig, "content-type": "application/json"}


def test_webhook_bad_signature_401(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    raw, headers = _signed({"data": {"order": {"order_id": order_id}}}, secret="wrong")
    resp = _client(Session).post("/webhooks/cashfree/wallet", content=raw, headers=headers)
    assert resp.status_code == 401
    assert _wallet(db) is None


def test_webhook_triggers_verified_credit_and_replay_is_safe(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    raw, headers = _signed(
        {"type": "PAYMENT_SUCCESS_WEBHOOK", "data": {"order": {"order_id": order_id, "order_amount": 999999}}}
    )
    client = _client(Session)
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        assert client.post("/webhooks/cashfree/wallet", content=raw, headers=headers).status_code == 200
        assert client.post("/webhooks/cashfree/wallet", content=raw, headers=headers).status_code == 200
    assert _wallet(db).balance_paise == 50_000
    assert db.query(VendorWalletTransaction).count() == 1


def test_webhook_unknown_order_ignored(env):
    _, Session = env
    raw, headers = _signed({"data": {"order": {"order_id": "SSP" + "a" * 24}}})
    with patch.object(cashfree_pg, "get_order") as get:
        resp = _client(Session).post("/webhooks/cashfree/wallet", content=raw, headers=headers)
    assert resp.status_code == 200
    get.assert_not_called()


def test_webhook_gateway_down_asks_retry(env):
    db, Session = env
    order_id = _pending_order(db, Session)
    raw, headers = _signed({"data": {"order": {"order_id": order_id}}})
    with patch.object(cashfree_pg, "get_order", side_effect=cashfree_pg.CashfreeError("timeout")):
        resp = _client(Session).post("/webhooks/cashfree/wallet", content=raw, headers=headers)
    assert resp.status_code == 503


def test_amount_to_paise_exact_only():
    assert cashfree_pg.amount_to_paise(500.0) == 50_000
    assert cashfree_pg.amount_to_paise("123.45") == 12_345
    assert cashfree_pg.amount_to_paise(1.005) is None
    assert cashfree_pg.amount_to_paise("abc") is None


# --- Withdrawal ----------------------------------------------------------------


def test_fee_slabs():
    assert withdrawal_crud.withdrawal_fee_paise(10_000) == 708
    assert withdrawal_crud.withdrawal_fee_paise(100_000) == 708
    assert withdrawal_crud.withdrawal_fee_paise(100_001) == 944
    assert withdrawal_crud.withdrawal_fee_paise(2_500_000) == 944
    assert withdrawal_crud.withdrawal_fee_paise(2_500_001) == 1_770


def test_withdrawal_disabled(env, monkeypatch):
    _, Session = env
    monkeypatch.setenv("WALLET_WITHDRAWAL_ENABLED", "0")
    resp = _vendor(Session).get("/vendor/wallet/withdrawals/preflight")
    assert resp.status_code == 503


def test_preflight_masks_bank_and_reports_limits(env):
    db, Session = env
    _fund(db, 300_000)
    body = _vendor(Session).get("/vendor/wallet/withdrawals/preflight").json()
    assert body["bankAccount"]["hasBankAccount"] is True
    assert body["bankAccount"]["maskedAccountNumber"] == "XXXXXXXX9012"
    assert ACCOUNT_NO not in json.dumps(body)
    assert body["availablePaise"] == 300_000
    assert body["minPaise"] == 10_000
    assert body["maxPaise"] == 2_500_000
    assert body["dailyRemainingPaise"] == 5_000_000
    assert body["feeSlabs"][0] == {"upToPaise": 100_000, "feePaise": 708}


def test_no_bank_details_prompts(env):
    db, Session = env
    db.query(User).filter(User.UID == VENDOR_UID).update({User.bankAccountNo: None})
    db.commit()
    _fund(db, 300_000)
    client = _vendor(Session)
    assert client.get("/vendor/wallet/withdrawals/preflight").json()["bankAccount"]["hasBankAccount"] is False
    otp = client.post("/vendor/wallet/withdrawals/otp")
    assert otp.status_code == 409
    assert otp.json()["detail"] == "BANK_DETAILS_REQUIRED"
    create = client.post(
        "/vendor/wallet/withdrawals",
        json={"amountPaise": 10_000, "otp": FIXED_OTP, "idempotencyKey": "wd-key-0001"},
    )
    assert create.status_code == 409
    assert create.json()["detail"] == "BANK_DETAILS_REQUIRED"


def _withdraw(client, amount=100_000, key="wd-key-0001", otp=FIXED_OTP, send_otp=True):
    if send_otp:
        assert client.post("/vendor/wallet/withdrawals/otp").status_code == 200
    return client.post(
        "/vendor/wallet/withdrawals",
        json={"amountPaise": amount, "otp": otp, "idempotencyKey": key},
    )


def test_withdrawal_holds_funds_and_emails_ops(env):
    db, Session = env
    _fund(db, 300_000)
    client = _vendor(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}) as mail:
        resp = _withdraw(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "REQUESTED"
    assert body["feePaise"] == 708
    assert body["totalPaise"] == 100_708
    assert body["maskedAccountNumber"] == "XXXXXXXX9012"
    assert ACCOUNT_NO not in resp.text
    wallet = _wallet(db)
    assert wallet.balance_paise == 300_000
    assert wallet.held_paise == 100_708
    kwargs = mail.call_args.kwargs
    assert kwargs["to_address"] == "openbidaccounts@wizzride.com"
    assert ACCOUNT_NO in kwargs["message"]
    assert "SBIN0001234" in kwargs["message"]
    assert body["withdrawalId"] in kwargs["message"]
    assert "/ops/paid?t=" in kwargs["message"] and "/ops/rejected?t=" in kwargs["message"]
    row = db.query(VendorWalletWithdrawal).one()
    assert row.ops_email_status == "SENT"
    assert row.ops_link_paid_jti_hash and row.ops_link_reject_jti_hash
    assert row.ops_link_expires_at is not None
    listed = client.get("/vendor/wallet/withdrawals").json()["items"]
    assert listed[0]["withdrawalId"] == body["withdrawalId"]
    assert ACCOUNT_NO not in json.dumps(listed)


def test_withdrawal_email_failure_still_recorded(env):
    db, Session = env
    _fund(db, 300_000)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "ERROR_AUTH_FAILED"}):
        resp = _withdraw(_vendor(Session))
    assert resp.status_code == 200
    db.expire_all()
    assert db.query(VendorWalletWithdrawal).one().ops_email_status == "FAILED"


def test_withdrawal_idempotent_retry_no_double_hold(env):
    db, Session = env
    _fund(db, 300_000)
    client = _vendor(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}) as mail:
        first = _withdraw(client)
        second = _withdraw(client, send_otp=False, otp="000000")
    assert second.status_code == 200
    assert second.json()["withdrawalId"] == first.json()["withdrawalId"]
    assert _wallet(db).held_paise == 100_708
    assert mail.call_count == 1


def test_withdrawal_wrong_otp_and_lockout(env):
    db, Session = env
    _fund(db, 300_000)
    client = _vendor(Session)
    client.post("/vendor/wallet/withdrawals/otp")
    details = []
    for n in range(5):
        r = _withdraw(client, otp="111111", key=f"wd-key-bad{n}", send_otp=False)
        details.append(r.json()["detail"])
    assert details[:4] == ["OTP_INVALID"] * 4
    assert details[4] == "OTP_LOCKED"
    assert _withdraw(client, key="wd-key-after", send_otp=False).json()["detail"] == "OTP_EXPIRED"
    assert _wallet(db).held_paise == 0


def test_withdrawal_insufficient_balance_keeps_otp(env):
    db, Session = env
    _fund(db, 100_000)
    client = _vendor(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}):
        r = _withdraw(client, amount=100_000)
        assert r.status_code == 422
        assert r.json()["detail"] == "INSUFFICIENT_BALANCE"
        ok = _withdraw(client, amount=90_000, key="wd-key-0002", send_otp=False)
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "amount,detail",
    [(9_999, "WITHDRAWAL_BELOW_MIN"), (2_500_001, "WITHDRAWAL_ABOVE_MAX")],
)
def test_withdrawal_amount_limits(env, amount, detail):
    db, Session = env
    _fund(db, 10_000_000)
    r = _withdraw(_vendor(Session), amount=amount)
    assert r.status_code == 422
    assert r.json()["detail"] == detail


def test_withdrawal_daily_limit(env):
    db, Session = env
    _fund(db, 10_000_000)
    client = _vendor(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}):
        assert _withdraw(client, amount=2_500_000, key="wd-key-day1").status_code == 200
        assert _withdraw(client, amount=2_500_000, key="wd-key-day2").status_code == 200
        r = _withdraw(client, amount=10_000, key="wd-key-day3")
    assert r.json()["detail"] == "WITHDRAWAL_DAILY_LIMIT_EXCEEDED"


# --- Ops ------------------------------------------------------------------------


def _requested(db, Session):
    _fund(db, 300_000)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}):
        return _withdraw(_vendor(Session)).json()["withdrawalId"]


def test_ops_requires_key(env):
    db, Session = env
    ref = _requested(db, Session)
    client = _client(Session)
    assert client.get("/internal/wallet/withdrawals").status_code == 403
    r = client.post(
        f"/internal/wallet/withdrawals/{ref}/paid",
        json={"utr": "UTR123456"},
        headers={"X-OpenBid-Wallet-Ops-Key": "wrong"},
    )
    assert r.status_code == 403
    assert _wallet(db).held_paise == 100_708


def test_ops_key_unset_fails_closed(env, monkeypatch):
    _, Session = env
    monkeypatch.delenv("WALLET_OPS_KEY")
    r = _client(Session).get("/internal/wallet/withdrawals", headers={"X-OpenBid-Wallet-Ops-Key": ""})
    assert r.status_code == 403


def test_ops_list_includes_full_bank_details(env):
    db, Session = env
    ref = _requested(db, Session)
    r = _client(Session).get(
        "/internal/wallet/withdrawals",
        params={"status": "REQUESTED"},
        headers={"X-OpenBid-Wallet-Ops-Key": OPS_KEY},
    )
    item = r.json()["items"][0]
    assert item["withdrawalId"] == ref
    assert item["accountNumber"] == ACCOUNT_NO
    assert item["vendorPhone"] == VENDOR


def test_ops_mark_paid_debits_hold(env):
    db, Session = env
    ref = _requested(db, Session)
    client = _client(Session)
    headers = {"X-OpenBid-Wallet-Ops-Key": OPS_KEY}
    r = client.post(f"/internal/wallet/withdrawals/{ref}/paid", json={"utr": "utr123456"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "PAID"
    assert r.json()["utr"] == "UTR123456"
    wallet = _wallet(db)
    assert wallet.balance_paise == 300_000 - 100_708
    assert wallet.held_paise == 0
    types_ = sorted(t.txn_type for t in db.query(VendorWalletTransaction).all())
    assert types_ == ["ADJUSTMENT", "WITHDRAWAL", "WITHDRAWAL_FEE"]
    again = client.post(f"/internal/wallet/withdrawals/{ref}/paid", json={"utr": "UTR123456"}, headers=headers)
    assert again.status_code == 200
    assert _wallet(db).balance_paise == 300_000 - 100_708
    reject = client.post(f"/internal/wallet/withdrawals/{ref}/rejected", json={"reason": "late"}, headers=headers)
    assert reject.status_code == 409


def test_ops_reject_releases_hold(env):
    db, Session = env
    ref = _requested(db, Session)
    r = _client(Session).post(
        f"/internal/wallet/withdrawals/{ref}/rejected",
        json={"reason": "Account name does not match"},
        headers={"X-OpenBid-Wallet-Ops-Key": OPS_KEY},
    )
    assert r.status_code == 200
    wallet = _wallet(db)
    assert wallet.balance_paise == 300_000
    assert wallet.held_paise == 0
    item = _vendor(Session).get("/vendor/wallet/withdrawals").json()["items"][0]
    assert item["status"] == "REJECTED"
    assert item["rejectionReason"] == "Account name does not match"


def test_vendor_cannot_call_ops_with_jwt(env):
    db, Session = env
    ref = _requested(db, Session)
    r = _vendor(Session).post(f"/internal/wallet/withdrawals/{ref}/paid", json={"utr": "UTR123456"})
    assert r.status_code == 403


def _ops_link_from_email(message: str, action: str) -> tuple[str, str]:
    """Return (path_with_query, token) for the signed ops link in the email body."""
    marker = f"/internal/wallet/withdrawals/"
    for line in message.splitlines():
        line = line.strip()
        if marker in line and f"/ops/{action}?t=" in line:
            # May be absolute URL; keep path+query for TestClient.
            idx = line.index(marker)
            url = line[idx:]
            token = url.split("?t=", 1)[1]
            return url, token
    raise AssertionError(f"no {action} ops link in email")


def test_ops_signed_link_paid_flow(env):
    db, Session = env
    _fund(db, 300_000)
    client = _client(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}) as mail:
        ref = _withdraw(_vendor(Session)).json()["withdrawalId"]
    path, token = _ops_link_from_email(mail.call_args.kwargs["message"], "paid")
    form = client.get(path)
    assert form.status_code == 200
    assert "Mark withdrawal paid" in form.text
    assert "name=\"utr\"" in form.text
    assert token in form.text
    posted = client.post(
        f"/internal/wallet/withdrawals/{ref}/ops/paid",
        data={"t": token, "utr": "HDFC123456"},
    )
    assert posted.status_code == 200
    assert "marked paid" in posted.text.lower()
    wallet = _wallet(db)
    assert wallet.held_paise == 0
    assert wallet.balance_paise == 300_000 - 100_708
    row = db.query(VendorWalletWithdrawal).one()
    assert row.status == "PAID"
    assert row.utr == "HDFC123456"
    assert row.ops_link_paid_jti_hash is None
    assert row.ops_link_reject_jti_hash is None
    reuse = client.get(path)
    assert reuse.status_code == 410
    assert "already used" in reuse.text.lower()


def test_ops_signed_link_reject_flow(env):
    db, Session = env
    _fund(db, 300_000)
    client = _client(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}) as mail:
        ref = _withdraw(_vendor(Session)).json()["withdrawalId"]
    path, token = _ops_link_from_email(mail.call_args.kwargs["message"], "rejected")
    assert client.get(path).status_code == 200
    posted = client.post(
        f"/internal/wallet/withdrawals/{ref}/ops/rejected",
        data={"t": token, "reason": "Name mismatch on transfer"},
    )
    assert posted.status_code == 200
    assert "rejected" in posted.text.lower()
    wallet = _wallet(db)
    assert wallet.balance_paise == 300_000
    assert wallet.held_paise == 0
    assert db.query(VendorWalletWithdrawal).one().status == "REJECTED"


def test_ops_signed_link_tampered_token(env):
    db, Session = env
    _fund(db, 300_000)
    client = _client(Session)
    with patch.object(withdrawal_crud, "send_email", return_value={"message": "SENT"}) as mail:
        _withdraw(_vendor(Session))
    path, _token = _ops_link_from_email(mail.call_args.kwargs["message"], "paid")
    bad = path[:-4] + "dead"
    r = client.get(bad)
    assert r.status_code == 403
    assert _wallet(db).held_paise == 100_708
    assert db.query(VendorWalletWithdrawal).one().status == "REQUESTED"


def test_ops_key_json_still_works_after_links_minted(env):
    db, Session = env
    ref = _requested(db, Session)
    r = _client(Session).post(
        f"/internal/wallet/withdrawals/{ref}/paid",
        json={"utr": "UTR999888"},
        headers={"X-OpenBid-Wallet-Ops-Key": OPS_KEY},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "PAID"


# --- Ledger holds -------------------------------------------------------------


def test_ledger_hold_release_consume(env):
    db, _ = env
    _fund(db, 10_000)
    ledger.hold_funds(db, user_uid=VENDOR_UID, amount_paise=6_000)
    db.commit()
    with pytest.raises(ledger.InsufficientWalletBalance):
        ledger.hold_funds(db, user_uid=VENDOR_UID, amount_paise=4_001)
    db.rollback()
    with pytest.raises(ledger.InsufficientWalletBalance):
        ledger.apply_wallet_entry(
            db,
            user_uid=VENDOR_UID,
            direction=ledger.DIRECTION_DEBIT,
            txn_type=ledger.TXN_TYPE_WITHDRAWAL,
            amount_paise=4_001,
            idempotency_key="no-hold",
        )
    db.rollback()
    ledger.apply_wallet_entry(
        db,
        user_uid=VENDOR_UID,
        direction=ledger.DIRECTION_DEBIT,
        txn_type=ledger.TXN_TYPE_WITHDRAWAL,
        amount_paise=6_000,
        idempotency_key="with-hold",
        consume_hold_paise=6_000,
    )
    db.commit()
    w = _wallet(db)
    assert (w.balance_paise, w.held_paise) == (4_000, 0)
    with pytest.raises(ledger.WalletLedgerError):
        ledger.release_held_funds(db, user_uid=VENDOR_UID, amount_paise=1)
