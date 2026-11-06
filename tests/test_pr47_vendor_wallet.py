"""
PR47 vendor wallet Phase 1 — ledger + JWT-owned balance/history endpoints.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
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
from app_v1.models.vendor_wallet import (  # noqa: E402
    VendorWallet,
    VendorWalletTransaction,
)
from app_v1.endpoints.vendor_wallet import router as wallet_router  # noqa: E402
from app_v1.services import vendor_wallet_ledger as ledger  # noqa: E402

VENDOR = "8637554388"
OTHER_VENDOR = "8637554399"
CUSTOMER = "7022359323"
VENDOR_UID = 1
OTHER_UID = 2
CUSTOMER_UID = 3

PR47_TABLES = [
    User.__table__,
    VendorWallet.__table__,
    VendorWalletTransaction.__table__,
]


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR47_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


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
    app.include_router(wallet_router)

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
    user = User(
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
        alsoVendor=kwargs.get("alsoVendor", True),
        vendorApproved=kwargs.get("vendorApproved", True),
        lockApp=kwargs.get("lockApp", False),
        customerRating="4.5",
        totalCustomerReviews=0,
        rating="4.5",
        totalNoOfReviews=0,
        joiningDate=date(2024, 1, 15),
        user_login_status="LOGGEDOUT",
    )
    db.add(user)
    db.commit()
    return user


def _credit(db, uid: int, amount: int, key: str, txn_type=ledger.TXN_TYPE_RECHARGE):
    txn = ledger.apply_wallet_entry(
        db,
        user_uid=uid,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=txn_type,
        amount_paise=amount,
        idempotency_key=key,
        description="Test credit",
    )
    db.commit()
    return txn


@pytest.fixture
def env():
    engine, Session = _memory_db()
    db = Session()
    try:
        _add_user(db, user_app_id=VENDOR, uid=VENDOR_UID)
        _add_user(db, user_app_id=OTHER_VENDOR, uid=OTHER_UID)
        _add_user(
            db,
            user_app_id=CUSTOMER,
            uid=CUSTOMER_UID,
            alsoVendor=False,
            vendorApproved=False,
        )
        yield db, Session
    finally:
        db.close()


# --- Auth / eligibility -----------------------------------------------------


def test_no_token_rejected(env):
    _, Session = env
    client = _client(Session, None)
    assert client.get("/vendor/wallet").status_code in (401, 403)
    assert client.get("/vendor/wallet/transactions").status_code in (401, 403)


def test_customer_not_eligible(env):
    _, Session = env
    client = _client(Session, _auth_user(CUSTOMER, CUSTOMER_UID))
    for path in ("/vendor/wallet", "/vendor/wallet/transactions"):
        resp = client.get(path)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_pending_vendor_not_eligible(env):
    db, Session = env
    db.query(User).filter(User.UID == VENDOR_UID).update({User.vendorApproved: False})
    db.commit()
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    resp = client.get("/vendor/wallet")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_locked_vendor_account_locked(env):
    db, Session = env
    db.query(User).filter(User.UID == VENDOR_UID).update({User.lockApp: True})
    db.commit()
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    resp = client.get("/vendor/wallet/transactions")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "ACCOUNT_LOCKED"


def test_missing_user_404(env):
    _, Session = env
    client = _client(Session, _auth_user("7999999999", 999))
    resp = client.get("/vendor/wallet")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"


# --- Balance ----------------------------------------------------------------


def test_balance_zero_without_wallet_row_and_no_row_created(env):
    db, Session = env
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    resp = client.get("/vendor/wallet")
    assert resp.status_code == 200
    assert resp.json() == {
        "balancePaise": 0,
        "heldPaise": 0,
        "availablePaise": 0,
        "currency": "INR",
    }
    assert db.query(VendorWallet).count() == 0


def test_balance_reflects_ledger_and_held(env):
    db, Session = env
    _credit(db, VENDOR_UID, 50_000, "k1")
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == VENDOR_UID).one()
    wallet.held_paise = 10_000
    db.commit()
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    body = client.get("/vendor/wallet").json()
    assert body["balancePaise"] == 50_000
    assert body["heldPaise"] == 10_000
    assert body["availablePaise"] == 40_000


def test_vendor_cannot_see_other_wallet(env):
    db, Session = env
    _credit(db, OTHER_UID, 99_900, "other-1")
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    assert client.get("/vendor/wallet").json()["balancePaise"] == 0
    assert client.get("/vendor/wallet/transactions").json()["items"] == []


# --- Ledger -----------------------------------------------------------------


def test_credit_then_debit_updates_balance(env):
    db, _ = env
    _credit(db, VENDOR_UID, 10_000, "c1")
    txn = ledger.apply_wallet_entry(
        db,
        user_uid=VENDOR_UID,
        direction=ledger.DIRECTION_DEBIT,
        txn_type=ledger.TXN_TYPE_WITHDRAWAL,
        amount_paise=2_500,
        idempotency_key="d1",
    )
    db.commit()
    assert txn.balance_after_paise == 7_500
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == VENDOR_UID).one()
    assert wallet.balance_paise == 7_500


def test_idempotent_replay_does_not_double_credit(env):
    db, _ = env
    first = _credit(db, VENDOR_UID, 10_000, "same-key")
    second = _credit(db, VENDOR_UID, 10_000, "same-key")
    assert first.txn_ref == second.txn_ref
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == VENDOR_UID).one()
    assert wallet.balance_paise == 10_000
    assert db.query(VendorWalletTransaction).count() == 1


def test_idempotency_key_of_other_wallet_rejected(env):
    db, _ = env
    _credit(db, OTHER_UID, 10_000, "shared-key")
    with pytest.raises(ledger.WalletLedgerError):
        _credit(db, VENDOR_UID, 10_000, "shared-key")


def test_debit_cannot_exceed_available(env):
    db, _ = env
    _credit(db, VENDOR_UID, 10_000, "c1")
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == VENDOR_UID).one()
    wallet.held_paise = 4_000
    db.commit()
    with pytest.raises(ledger.InsufficientWalletBalance):
        ledger.apply_wallet_entry(
            db,
            user_uid=VENDOR_UID,
            direction=ledger.DIRECTION_DEBIT,
            txn_type=ledger.TXN_TYPE_WITHDRAWAL,
            amount_paise=6_001,
            idempotency_key="d-too-much",
        )
    db.rollback()
    wallet = db.query(VendorWallet).filter(VendorWallet.user_uid == VENDOR_UID).one()
    assert wallet.balance_paise == 10_000
    assert db.query(VendorWalletTransaction).count() == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount_paise": 0},
        {"amount_paise": -5},
        {"amount_paise": 10.5},
        {"amount_paise": True},
        {"direction": "SIDEWAYS"},
        {"txn_type": "GIFT"},
        {"idempotency_key": "  "},
    ],
)
def test_invalid_ledger_requests_rejected(env, kwargs):
    db, _ = env
    args = dict(
        user_uid=VENDOR_UID,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=ledger.TXN_TYPE_RECHARGE,
        amount_paise=100,
        idempotency_key="k",
    )
    args.update(kwargs)
    with pytest.raises(ledger.WalletLedgerError):
        ledger.apply_wallet_entry(db, **args)


def test_ledger_locks_wallet_row(env):
    db, _ = env
    from sqlalchemy.orm.query import Query

    called = {"for_update": False}
    original = Query.with_for_update

    def _spy(self, *args, **kwargs):
        called["for_update"] = True
        return original(self, *args, **kwargs)

    with patch.object(Query, "with_for_update", _spy):
        _credit(db, VENDOR_UID, 100, "lock-check")
    assert called["for_update"] is True


# --- History ----------------------------------------------------------------


def test_history_newest_first_and_contract(env):
    db, Session = env
    _credit(db, VENDOR_UID, 1_000, "h1")
    _credit(db, VENDOR_UID, 2_000, "h2")
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    body = client.get("/vendor/wallet/transactions").json()
    assert body["nextCursor"] is None
    assert [i["amountPaise"] for i in body["items"]] == [2_000, 1_000]
    item = body["items"][0]
    assert set(item.keys()) == {
        "transactionId",
        "direction",
        "type",
        "status",
        "amountPaise",
        "balanceAfterPaise",
        "description",
        "createdAt",
    }
    assert item["transactionId"].startswith("WT")
    assert item["balanceAfterPaise"] == 3_000
    assert "h2" not in str(body)


def test_history_pagination(env):
    db, Session = env
    for n in range(5):
        _credit(db, VENDOR_UID, 100 * (n + 1), f"p{n}")
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    first = client.get("/vendor/wallet/transactions", params={"limit": 2}).json()
    assert [i["amountPaise"] for i in first["items"]] == [500, 400]
    assert first["nextCursor"]
    second = client.get(
        "/vendor/wallet/transactions",
        params={"limit": 2, "cursor": first["nextCursor"]},
    ).json()
    assert [i["amountPaise"] for i in second["items"]] == [300, 200]
    third = client.get(
        "/vendor/wallet/transactions",
        params={"limit": 2, "cursor": second["nextCursor"]},
    ).json()
    assert [i["amountPaise"] for i in third["items"]] == [100]
    assert third["nextCursor"] is None


def test_history_date_filter(env):
    db, Session = env
    old = _credit(db, VENDOR_UID, 100, "old")
    old.created_at = datetime(2026, 1, 10, 12, 0, 0)
    new = _credit(db, VENDOR_UID, 200, "new")
    new.created_at = datetime(2026, 3, 5, 23, 59, 0)
    db.commit()
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    body = client.get(
        "/vendor/wallet/transactions",
        params={"startDate": "2026-03-01", "endDate": "2026-03-05"},
    ).json()
    assert [i["amountPaise"] for i in body["items"]] == [200]


@pytest.mark.parametrize(
    "params,detail",
    [
        ({"startDate": "2026-01-01"}, None),
        ({"startDate": "2026-02-01", "endDate": "2026-01-01"}, None),
        ({"startDate": "bad", "endDate": "2026-01-01"}, None),
        ({"startDate": "2020-01-01", "endDate": "2026-01-01"}, "WALLET_RANGE_TOO_LARGE"),
        ({"cursor": "abc"}, "INVALID_CURSOR"),
        ({"cursor": "0"}, "INVALID_CURSOR"),
        ({"limit": 0}, None),
        ({"limit": 51}, None),
    ],
)
def test_history_invalid_params_422(env, params, detail):
    _, Session = env
    client = _client(Session, _auth_user(VENDOR, VENDOR_UID))
    resp = client.get("/vendor/wallet/transactions", params=params)
    assert resp.status_code == 422
    if detail:
        assert resp.json()["detail"] == detail


def test_db_failure_safe_500():
    from fastapi import HTTPException
    from app_v1.crud import vendor_wallet as wallet_crud

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            raise SQLAlchemyError("db down secret")

    class FakeDb:
        rolled_back = False

        def query(self, *a, **k):
            return FakeQuery()

        def rollback(self):
            self.rolled_back = True

    fake_db = FakeDb()
    with pytest.raises(HTTPException) as exc:
        wallet_crud.get_vendor_wallet_summary(fake_db, _auth_user(VENDOR, VENDOR_UID))
    assert exc.value.status_code == 500
    assert exc.value.detail == "WALLET_QUERY_FAILED"
    assert fake_db.rolled_back is True
