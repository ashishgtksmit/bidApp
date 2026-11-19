"""Admin Console (/admin/v1) against the real bidApp models and money CRUD."""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta
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
os.environ["ADMIN_JWT_SECRET"] = "admin-test-secret-" + "x" * 40

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.admin import admin_router  # noqa: E402
from app_v1.admin.models import AdminAuditLog, AdminUser  # noqa: E402
from app_v1.admin.security import hash_password  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.vendor_wallet import (  # noqa: E402
    VendorWallet,
    VendorWalletTransaction,
    VendorWalletWithdrawal,
)
from app_v1.models.vendor_subscription import (  # noqa: E402
    VendorSubscriptionPeriod,
    VendorSubscriptionPlan,
    VendorSubscriptionState,
)

PASSWORD = "Correct-Horse-9"
TABLES = [
    AdminUser.__table__, AdminAuditLog.__table__, User.__table__,
    VendorWallet.__table__, VendorWalletTransaction.__table__, VendorWalletWithdrawal.__table__,
    VendorSubscriptionPlan.__table__, VendorSubscriptionPeriod.__table__, VendorSubscriptionState.__table__,
]


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_db] = _db
    # No real pushes / emails / snapshot calls from tests.
    import app_v1.admin.routes_partners as rp
    monkeypatch.setattr(rp, "request_snapshot_refresh", lambda *a, **k: True)
    db = Session()
    now = datetime.utcnow()
    for email, role in (("fin@openbid.in", "finance"), ("ops@openbid.in", "operations")):
        db.add(AdminUser(email=email, full_name=role, role=role, password_hash=hash_password(PASSWORD), is_active=True,
                         must_change_password=False, token_version=1, failed_logins=0, created_at=now, updated_at=now))
    vendor = User(UID=101, userAppId="9000000002", password="x", fullName="Tenzing Travels", emailId="t@x.in", dob="1990-01-01",
                  city="Gangtok", alsoVendor=True, vendorApproved=False, lockApp=False, rating="5", totalNoOfReviews=0)
    db.add(vendor)
    db.flush()
    db.add(VendorWallet(user_uid=vendor.UID, balance_paise=251_000, held_paise=201_000, currency="INR"))
    db.add(VendorWalletWithdrawal(
        withdrawal_ref="WDTEST000001", user_uid=vendor.UID, amount_paise=200_000, fee_paise=1_000,
        status="REQUESTED", client_idempotency_key="k1", bank_account_holder_name="Tenzing",
        bank_account_no="123456789012", bank_ifsc="SBIN0000001", requested_at=now, source="VENDOR"))
    db.add(VendorWalletWithdrawal(
        withdrawal_ref="WDTEST000002", user_uid=vendor.UID, amount_paise=0, fee_paise=0,
        status="PAID", client_idempotency_key="k2", bank_account_holder_name="Tenzing",
        bank_account_no="123456789012", bank_ifsc="SBIN0000001", requested_at=now - timedelta(days=3)))
    db.add(VendorSubscriptionPlan(code="MONTHLY", name="Monthly", price_paise=50_000, duration_days=30, active=True))
    db.commit()
    yield TestClient(app), db, vendor.UID
    db.close()


def _login(client, email):
    r = client.post("/admin/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


def test_withdrawal_paid_uses_real_ledger(env):
    client, db, uid = env
    h = _login(client, "fin@openbid.in")
    assert client.post("/admin/v1/withdrawals/WDTEST000001/paid", headers=_login(client, "ops@openbid.in"),
                       json={"utr": "UTR123456789"}).status_code == 403
    r = client.post("/admin/v1/withdrawals/WDTEST000001/paid", headers=h, json={"utr": "UTR123456789"})
    assert r.status_code == 200, r.text
    db.expire_all()
    w = db.query(VendorWallet).filter(VendorWallet.user_uid == uid).one()
    row = db.query(VendorWalletWithdrawal).filter(VendorWalletWithdrawal.withdrawal_ref == "WDTEST000001").one()
    assert row.status == "PAID" and row.utr == "UTR123456789"
    assert w.held_paise == 0 and w.balance_paise == 50_000
    assert db.query(AdminAuditLog).filter(AdminAuditLog.action == "withdrawal.paid").count() == 1
    # Same UTR again is idempotent in bidApp's ledger: no second debit, no second audit row.
    again = client.post("/admin/v1/withdrawals/WDTEST000001/paid", headers=h, json={"utr": "UTR123456789"})
    assert again.status_code == 200
    db.expire_all()
    assert db.query(VendorWallet).filter(VendorWallet.user_uid == uid).one().balance_paise == 50_000
    assert db.query(AdminAuditLog).filter(AdminAuditLog.action == "withdrawal.paid").count() == 1


def test_withdrawal_reject_refunds_via_real_ledger(env):
    client, db, uid = env
    h = _login(client, "fin@openbid.in")
    r = client.post("/admin/v1/withdrawals/WDTEST000001/rejected", headers=h, json={"reason": "Account closed"})
    assert r.status_code == 200, r.text
    db.expire_all()
    w = db.query(VendorWallet).filter(VendorWallet.user_uid == uid).one()
    assert w.held_paise == 0 and w.balance_paise == 251_000


def test_withdrawals_list(env):
    client, _db, _uid = env
    out = client.get("/admin/v1/withdrawals?status=REQUESTED", headers=_login(client, "fin@openbid.in")).json()
    assert out["total"] == 1 and out["items"][0]["payoutPaise"] == 200_000


def test_plan_and_grant_use_real_subscription_crud(env):
    client, db, uid = env
    h = _login(client, "fin@openbid.in")
    r = client.put("/admin/v1/subscriptions/plans/MONTHLY", headers=h, json={"pricePaise": 59_900})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(VendorSubscriptionPlan).filter_by(code="MONTHLY").one().price_paise == 59_900
    g = client.post("/admin/v1/subscriptions/grant", headers=h,
                    json={"userAppId": "9000000002", "days": 15, "reason": "Referral bonus"})
    assert g.status_code == 200, g.text
    db.expire_all()
    s = db.query(VendorSubscriptionState).filter_by(user_uid=uid).one()
    assert s.current_period_end > datetime.utcnow() + timedelta(days=14)
    subs = client.get("/admin/v1/subscriptions/subscribers?status=active", headers=h).json()
    assert subs["total"] == 1


def test_approve_vendor_on_real_user_model(env):
    client, db, uid = env
    r = client.post(f"/admin/v1/vendors/{uid}/approve", headers=_login(client, "ops@openbid.in"))
    assert r.status_code == 200, r.text
    db.expire_all()
    u = db.query(User).filter(User.UID == uid).one()
    assert u.vendorApproved and "1" in (u.tags or "").split(",")
