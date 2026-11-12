"""
Single wallet shared by the customer and vendor sides of the app.

``/customer/wallet/*`` and ``/vendor/wallet/*`` read and move the same
balance; recharges, withdrawals and ledger rows carry ``source``.

In-memory SQLite; Cashfree, SMS and SMTP are mocked.
"""

from __future__ import annotations

import csv
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
    VendorBankChangeOtpChallenge,
    VendorBankVerification,
    VendorWallet,
    VendorWalletOtpChallenge,
    VendorWalletRecharge,
    VendorWalletTransaction,
    VendorWalletWithdrawal,
)
from app_v1.crud import vendor_wallet_statement as statement_crud  # noqa: E402
from app_v1.crud import vendor_wallet_withdrawal as withdrawal_crud  # noqa: E402
from app_v1.endpoints.customer_wallet import router as customer_router  # noqa: E402
from app_v1.endpoints.vendor_wallet import router as vendor_router  # noqa: E402
from app_v1.services import cashfree_pg, wallet_notifications  # noqa: E402
from app_v1.services import vendor_wallet_ledger as ledger  # noqa: E402

VENDOR = "8637554388"
VENDOR_UID = 1
CUSTOMER = "8637554377"
CUSTOMER_UID = 3
PENDING = "8637554366"
PENDING_UID = 4
LOCKED = "8637554355"
LOCKED_UID = 5
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
    VendorBankChangeOtpChallenge.__table__,
    VendorBankVerification.__table__,
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in {
        "WALLET_RECHARGE_ENABLED": "1",
        "WALLET_WITHDRAWAL_ENABLED": "1",
        "CASHFREE_ENV": "SANDBOX",
        "CASHFREE_CLIENT_ID": "unit-test-client",
        "CASHFREE_CLIENT_SECRET": "unit-test-secret",
        "CASHFREE_WALLET_NOTIFY_URL": "https://api.example.com/webhooks/cashfree/wallet",
        "WALLET_OPS_KEY": "unit-test-wallet-ops-key",
        "OTP_TEST_BYPASS_SMS": "1",
        "OTP_TEST_FIXED_OTP": FIXED_OTP,
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("BANK_CHANGE_OTP_REQUIRED", "BANK_VERIFICATION_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(wallet_notifications, "notify_vendor_wallet", lambda *a: None)


@pytest.fixture
def ops_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(withdrawal_crud, "send_email", lambda **kw: sent.append(kw) or {"message": "SENT"})
    return sent


def _auth(user_app_id, uid):
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"s-{user_app_id}",
        user_app_id=user_app_id,
        account_session_id="sess",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )


def _add_user(db, user_app_id, uid, *, also_vendor, approved, lock=False, bank=True):
    db.add(
        User(
            UID=uid,
            userAppId=user_app_id,
            password="secret",
            alternateNumber="1000000000",
            fullName="Wallet User",
            emailId=f"{user_app_id}@example.com",
            dob="1990-01-01",
            city="Gangtok",
            gender="Male",
            profilePicture="images/profilepic_male.png",
            alsoVendor=also_vendor,
            vendorApproved=approved,
            lockApp=lock,
            customerRating="4.5",
            totalCustomerReviews=0,
            rating="4.5",
            totalNoOfReviews=0,
            joiningDate=date(2024, 1, 15),
            user_login_status="LOGGEDOUT",
            bankAccountHolderName="Wallet User" if bank else None,
            bankAccountNo=ACCOUNT_NO if bank else None,
            bankIFSC="SBIN0001234" if bank else None,
            bankName="State Bank of India" if bank else None,
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
    _add_user(db, VENDOR, VENDOR_UID, also_vendor=True, approved=True)
    _add_user(db, CUSTOMER, CUSTOMER_UID, also_vendor=False, approved=False)
    _add_user(db, PENDING, PENDING_UID, also_vendor=True, approved=False)
    _add_user(db, LOCKED, LOCKED_UID, also_vendor=False, approved=False, lock=True)
    try:
        yield db, Session
    finally:
        db.close()


def _client(Session, user_app_id, uid):
    app = FastAPI()
    app.include_router(vendor_router)
    app.include_router(customer_router)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: _auth(user_app_id, uid)
    return TestClient(app)


def _fund(db, uid, paise, key):
    ledger.apply_wallet_entry(
        db,
        user_uid=uid,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=ledger.TXN_TYPE_ADJUSTMENT,
        amount_paise=paise,
        idempotency_key=key,
    )
    db.commit()


def _order(order_id, amount, status="ACTIVE"):
    return {
        "order_id": order_id,
        "cf_order_id": 99,
        "order_amount": amount,
        "order_currency": "INR",
        "order_status": status,
        "payment_session_id": "session_abc",
    }


def _recharge(client, prefix, key):
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _order(kw["order_id"], 500.0)):
        created = client.post(f"{prefix}/recharges", json={"amountPaise": 50_000, "idempotencyKey": key})
    assert created.status_code == 200, created.text
    order_id = created.json()["orderId"]
    with patch.object(cashfree_pg, "get_order", return_value=_order(order_id, 500.0, "PAID")):
        paid = client.get(f"{prefix}/recharges/{order_id}")
    assert paid.status_code == 200, paid.text
    return created.json(), paid.json()


def _withdraw(client, prefix, key, amount=10_000):
    assert client.post(f"{prefix}/withdrawals/otp").status_code == 200
    return client.post(
        f"{prefix}/withdrawals",
        json={"amountPaise": amount, "otp": FIXED_OTP, "idempotencyKey": key},
    )


# --- Shared balance ---------------------------------------------------------------


def test_both_sides_see_the_same_balance(env):
    db, Session = env
    _fund(db, VENDOR_UID, 70_000, "seed-1")
    client = _client(Session, VENDOR, VENDOR_UID)
    vendor_side = client.get("/vendor/wallet").json()
    customer_side = client.get("/customer/wallet").json()
    assert vendor_side["availablePaise"] == customer_side["availablePaise"] == 70_000
    assert db.query(VendorWallet).count() == 1


def test_recharge_tagged_by_side_and_shared(env):
    db, Session = env
    client = _client(Session, VENDOR, VENDOR_UID)
    created_c, paid_c = _recharge(client, "/customer/wallet", "cust-key-0001")
    created_v, paid_v = _recharge(client, "/vendor/wallet", "vend-key-0001")
    assert created_c["source"] == "CUSTOMER" and paid_c["status"] == "PAID"
    assert created_v["source"] == "VENDOR" and paid_v["status"] == "PAID"
    assert client.get("/customer/wallet").json()["availablePaise"] == 100_000
    items = client.get("/vendor/wallet/transactions").json()["items"]
    assert sorted(i["source"] for i in items) == ["CUSTOMER", "VENDOR"]
    same = client.get("/customer/wallet/transactions").json()["items"]
    assert [i["transactionId"] for i in same] == [i["transactionId"] for i in items]


def test_ops_adjustment_has_no_source(env):
    db, Session = env
    _fund(db, CUSTOMER_UID, 5_000, "seed-2")
    items = _client(Session, CUSTOMER, CUSTOMER_UID).get("/customer/wallet/transactions").json()["items"]
    assert items[0]["source"] is None


def test_invalid_source_rejected(env):
    db, _ = env
    with pytest.raises(ledger.WalletLedgerError):
        ledger.apply_wallet_entry(
            db,
            user_uid=CUSTOMER_UID,
            direction=ledger.DIRECTION_CREDIT,
            txn_type=ledger.TXN_TYPE_ADJUSTMENT,
            amount_paise=100,
            idempotency_key="bad-source",
            source="OPS",
        )


# --- Eligibility ------------------------------------------------------------------


def test_customer_side_open_to_plain_customers(env):
    _, Session = env
    client = _client(Session, CUSTOMER, CUSTOMER_UID)
    assert client.get("/customer/wallet").status_code == 200
    assert client.get("/customer/wallet/withdrawals").status_code == 200
    vendor_side = client.get("/vendor/wallet")
    assert vendor_side.status_code == 403
    assert vendor_side.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_locked_user_blocked_on_customer_side(env):
    _, Session = env
    resp = _client(Session, LOCKED, LOCKED_UID).get("/customer/wallet")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "ACCOUNT_LOCKED"


def test_missing_user_404(env):
    _, Session = env
    resp = _client(Session, "8637550000", 99).get("/customer/wallet")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"


# --- Withdrawals ------------------------------------------------------------------


def test_customer_withdrawal_tagged_and_ops_email_shows_side(env, ops_mail):
    db, Session = env
    _fund(db, CUSTOMER_UID, 50_000, "seed-3")
    client = _client(Session, CUSTOMER, CUSTOMER_UID)
    preflight = client.get("/customer/wallet/withdrawals/preflight").json()
    assert preflight["withdrawalBlockedReason"] is None
    resp = _withdraw(client, "/customer/wallet", "wd-cust-0001")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "CUSTOMER"
    assert db.query(VendorWalletWithdrawal).one().source == "CUSTOMER"
    body = ops_mail[0]["message"]
    assert "Requested from    : Customer side of the app" in body
    assert ACCOUNT_NO in body
    listed = client.get("/customer/wallet/withdrawals").json()["items"]
    assert listed[0]["source"] == "CUSTOMER"


def test_vendor_withdrawal_tagged_vendor(env, ops_mail):
    db, Session = env
    _fund(db, VENDOR_UID, 50_000, "seed-4")
    resp = _withdraw(_client(Session, VENDOR, VENDOR_UID), "/vendor/wallet", "wd-vend-0001")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "VENDOR"
    assert "Requested from    : Vendor side of the app" in ops_mail[0]["message"]


def test_pending_vendor_can_view_and_recharge_but_not_withdraw(env, ops_mail):
    db, Session = env
    _fund(db, PENDING_UID, 50_000, "seed-5")
    client = _client(Session, PENDING, PENDING_UID)
    assert client.get("/customer/wallet").json()["availablePaise"] == 50_000
    _recharge(client, "/customer/wallet", "pend-key-0001")
    preflight = client.get("/customer/wallet/withdrawals/preflight").json()
    assert preflight["withdrawalBlockedReason"] == "VENDOR_ACCESS_RESTRICTED"
    otp = client.post("/customer/wallet/withdrawals/otp")
    assert otp.status_code == 403 and otp.json()["detail"] == "VENDOR_ACCESS_RESTRICTED"
    create = client.post(
        "/customer/wallet/withdrawals",
        json={"amountPaise": 10_000, "otp": FIXED_OTP, "idempotencyKey": "wd-pend-0001"},
    )
    assert create.status_code == 403 and create.json()["detail"] == "VENDOR_ACCESS_RESTRICTED"
    assert db.query(VendorWalletWithdrawal).count() == 0
    assert ops_mail == []


def test_customer_without_bank_gets_bank_required(env):
    db, Session = env
    db.query(User).filter(User.UID == CUSTOMER_UID).update(
        {User.bankAccountNo: None, User.bankIFSC: None, User.bankAccountHolderName: None, User.bankName: None}
    )
    db.commit()
    resp = _client(Session, CUSTOMER, CUSTOMER_UID).post("/customer/wallet/withdrawals/otp")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "BANK_DETAILS_REQUIRED"


# --- Statement --------------------------------------------------------------------


def test_customer_statement_has_side_column(env, monkeypatch):
    db, Session = env
    sent = []

    def fake_send(**kw):
        with open(kw["attachment_path"], newline="", encoding="utf-8") as handle:
            kw["csv_rows"] = list(csv.reader(handle))
        sent.append(kw)
        return {"message": "SENT"}

    monkeypatch.setattr(statement_crud, "send_email", fake_send)
    client = _client(Session, VENDOR, VENDOR_UID)
    _recharge(client, "/customer/wallet", "stmt-key-0001")
    today = db.query(VendorWalletTransaction).first().created_at.date().isoformat()
    resp = client.post("/customer/wallet/statements", json={"startDate": today, "endDate": today})
    assert resp.status_code == 200, resp.text
    rows = sent[0]["csv_rows"]
    assert rows[0][4] == "Side"
    assert rows[1][4] == "Customer"
