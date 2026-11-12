"""
PR50 customer wallet Phase 1 — separate customer ledger + balance/history.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime
from pathlib import Path

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
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.customer_wallet import (  # noqa: E402
    CustomerWallet,
    CustomerWalletTransaction,
)
from app_v1.models.vendor_wallet import (  # noqa: E402
    VendorWallet,
    VendorWalletTransaction,
)
from app_v1.endpoints.customer_wallet import router as customer_router  # noqa: E402
from app_v1.endpoints.vendor_wallet import router as vendor_router  # noqa: E402
from app_v1.services import vendor_wallet_ledger as ledger  # noqa: E402

CUSTOMER = "7022359323"
VENDOR = "8637554388"
CUSTOMER_UID = 3
VENDOR_UID = 1

TABLES = [
    User.__table__,
    VendorWallet.__table__,
    VendorWalletTransaction.__table__,
    CustomerWallet.__table__,
    CustomerWalletTransaction.__table__,
]

CUSTOMER_MODELS = {
    "wallet_model": CustomerWallet,
    "txn_model": CustomerWalletTransaction,
}


def _auth_user(user_app_id: str, uid: int) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"test-auth-subject-{user_app_id}",
        user_app_id=str(user_app_id),
        account_session_id="test-account-session",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )


def _client(Session, auth: AuthenticatedUser | None):
    app = FastAPI()
    app.include_router(customer_router)
    app.include_router(vendor_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    if auth is not None:
        app.dependency_overrides[get_current_user] = lambda: auth
    return TestClient(app)


def _add_user(db, *, user_app_id: str, uid: int, **kwargs):
    db.add(
        User(
            UID=uid,
            userAppId=user_app_id,
            password="secret",
            alternateNumber="1000000000",
            fullName="User",
            emailId=f"{user_app_id}@example.com",
            dob="1990-01-01",
            city="Gangtok",
            gender="Male",
            profilePicture="images/profilepic_male.png",
            alsoVendor=kwargs.get("alsoVendor", False),
            vendorApproved=kwargs.get("vendorApproved", False),
            lockApp=kwargs.get("lockApp", False),
            customerRating="4.5",
            totalCustomerReviews=0,
            rating="4.5",
            totalNoOfReviews=0,
            joiningDate=date(2024, 1, 15),
            user_login_status="LOGGEDOUT",
        )
    )
    db.commit()


def _credit(db, uid: int, amount: int, key: str, *, customer: bool = True):
    txn = ledger.apply_wallet_entry(
        db,
        user_uid=uid,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=ledger.TXN_TYPE_ADJUSTMENT,
        amount_paise=amount,
        idempotency_key=key,
        description="Test credit",
        **(CUSTOMER_MODELS if customer else {}),
    )
    db.commit()
    return txn


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
    try:
        _add_user(db, user_app_id=CUSTOMER, uid=CUSTOMER_UID)
        _add_user(
            db,
            user_app_id=VENDOR,
            uid=VENDOR_UID,
            alsoVendor=True,
            vendorApproved=True,
        )
        yield db, Session
    finally:
        db.close()


# --- Eligibility ---------------------------------------------------------------


def test_no_token_rejected(env):
    _, Session = env
    client = _client(Session, None)
    assert client.get("/customer/wallet").status_code in (401, 403)
    assert client.get("/customer/wallet/transactions").status_code in (401, 403)


def test_plain_customer_gets_empty_wallet(env):
    _, Session = env
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    resp = client.get("/customer/wallet")
    assert resp.status_code == 200
    assert resp.json() == {
        "balancePaise": 0,
        "heldPaise": 0,
        "availablePaise": 0,
        "currency": "INR",
    }
    resp = client.get("/customer/wallet/transactions")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "nextCursor": None}


def test_vendor_also_has_customer_wallet(env):
    _, Session = env
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    assert client.get("/customer/wallet").status_code == 200


def test_pending_vendor_can_use_customer_wallet(env):
    db, Session = env
    db.query(User).filter(User.UID == VENDOR_UID).update({User.vendorApproved: False})
    db.commit()
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    assert client.get("/customer/wallet").status_code == 200
    assert client.get("/vendor/wallet").status_code == 403


def test_locked_account(env):
    db, Session = env
    db.query(User).filter(User.UID == CUSTOMER_UID).update({User.lockApp: True})
    db.commit()
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    for path in ("/customer/wallet", "/customer/wallet/transactions"):
        resp = client.get(path)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "ACCOUNT_LOCKED"


def test_missing_user_404(env):
    _, Session = env
    client = _client(Session, _auth_user("7999999999", 999))
    resp = client.get("/customer/wallet")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"


def test_read_does_not_create_wallet_row(env):
    db, Session = env
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    client.get("/customer/wallet")
    assert db.query(CustomerWallet).count() == 0


# --- Separation from the vendor wallet --------------------------------------


def test_customer_and_vendor_balances_are_separate(env):
    db, Session = env
    _credit(db, VENDOR_UID, 50000, "vendor-1", customer=False)
    _credit(db, VENDOR_UID, 1500, "customer-1")
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))

    assert client.get("/vendor/wallet").json()["balancePaise"] == 50000
    assert client.get("/customer/wallet").json()["balancePaise"] == 1500

    vendor_items = client.get("/vendor/wallet/transactions").json()["items"]
    customer_items = client.get("/customer/wallet/transactions").json()["items"]
    assert [i["amountPaise"] for i in vendor_items] == [50000]
    assert [i["amountPaise"] for i in customer_items] == [1500]
    assert vendor_items[0]["transactionId"].startswith("WT")
    assert customer_items[0]["transactionId"].startswith("CT")


def test_customer_ledger_idempotent_and_debit_guarded(env):
    db, _ = env
    first = _credit(db, CUSTOMER_UID, 1000, "c-key")
    again = _credit(db, CUSTOMER_UID, 1000, "c-key")
    assert first.id == again.id
    with pytest.raises(ledger.InsufficientWalletBalance):
        ledger.apply_wallet_entry(
            db,
            user_uid=CUSTOMER_UID,
            direction=ledger.DIRECTION_DEBIT,
            txn_type=ledger.TXN_TYPE_ADJUSTMENT,
            amount_paise=1001,
            idempotency_key="c-debit",
            **CUSTOMER_MODELS,
        )
    db.rollback()
    wallet = db.query(CustomerWallet).filter(CustomerWallet.user_uid == CUSTOMER_UID).one()
    assert wallet.balance_paise == 1000
    assert db.query(VendorWallet).count() == 0


def test_customer_hold_and_release(env):
    db, _ = env
    _credit(db, CUSTOMER_UID, 1000, "c-hold-credit")
    ledger.hold_funds(db, user_uid=CUSTOMER_UID, amount_paise=400, wallet_model=CustomerWallet)
    db.commit()
    wallet = db.query(CustomerWallet).one()
    assert wallet.held_paise == 400
    ledger.release_held_funds(
        db, user_uid=CUSTOMER_UID, amount_paise=400, wallet_model=CustomerWallet
    )
    db.commit()
    assert db.query(CustomerWallet).one().held_paise == 0


# --- History ------------------------------------------------------------------


def test_paging(env):
    db, Session = env
    for i in range(3):
        _credit(db, CUSTOMER_UID, 100 + i, f"page-{i}")
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    first = client.get("/customer/wallet/transactions", params={"limit": 2}).json()
    assert [i["amountPaise"] for i in first["items"]] == [102, 101]
    assert first["nextCursor"]
    second = client.get(
        "/customer/wallet/transactions",
        params={"limit": 2, "cursor": first["nextCursor"]},
    ).json()
    assert [i["amountPaise"] for i in second["items"]] == [100]
    assert second["nextCursor"] is None


def test_date_range(env):
    db, Session = env
    old = _credit(db, CUSTOMER_UID, 100, "range-old")
    new = _credit(db, CUSTOMER_UID, 200, "range-new")
    old.created_at = datetime(2026, 9, 15, 10, 0)
    new.created_at = datetime(2026, 10, 31, 23, 59)
    db.commit()
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    resp = client.get(
        "/customer/wallet/transactions",
        params={"startDate": "2026-10-01", "endDate": "2026-10-31"},
    )
    assert [i["amountPaise"] for i in resp.json()["items"]] == [200]


@pytest.mark.parametrize(
    "params,detail",
    [
        ({"cursor": "abc"}, "INVALID_CURSOR"),
        ({"startDate": "2026-10-01"}, None),
        ({"startDate": "2024-01-01", "endDate": "2026-10-31"}, "WALLET_RANGE_TOO_LARGE"),
    ],
)
def test_invalid_queries_422(env, params, detail):
    _, Session = env
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    resp = client.get("/customer/wallet/transactions", params=params)
    assert resp.status_code == 422
    if detail:
        assert resp.json()["detail"] == detail
