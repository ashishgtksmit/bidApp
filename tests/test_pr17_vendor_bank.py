"""
PR17 vendor bank view/update — JWT-owned GET/PUT bank text endpoints.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.crud import user as user_crud  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402

CUSTOMER_ID = "7022359323"
PENDING_VENDOR = "8637554387"
APPROVED_VENDOR = "8637554388"
LOCKED_USER = "7000000001"
OTHER_USER = "7000000002"
MISSING_USER = "7999999999"

PR17_TABLES = [User.__table__]


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR17_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr17_client(engine, Session, user_id: str | None):
    app = FastAPI()
    app.include_router(user_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    if user_id is not None:
        app.dependency_overrides[get_current_user_id] = lambda: user_id
        app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app)


def _add_user(db, *, user_app_id: str, uid: int, **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="1000000000",
        fullName=kwargs.get("fullName", "User"),
        emailId=f"{user_app_id}@example.com",
        dob=kwargs.get("dob", "1990-01-01"),
        city=kwargs.get("city", "Gangtok"),
        gender=kwargs.get("gender", "Male"),
        profilePicture=kwargs.get("profilePicture", "images/profilepic_male.png"),
        alsoVendor=kwargs.get("alsoVendor", True),
        vendorApproved=kwargs.get("vendorApproved", True),
        lockApp=kwargs.get("lockApp", False),
        customerRating="4.5",
        totalCustomerReviews=0,
        rating=kwargs.get("rating", "4.5"),
        totalNoOfReviews=kwargs.get("totalNoOfReviews", 3),
        fcmToken=kwargs.get("fcmToken", "secret-fcm-token-should-not-leak"),
        joiningDate=kwargs.get("joiningDate", date(2024, 1, 15)),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 12),
        user_login_status="LOGGEDOUT",
        cityPreferences=kwargs.get("cityPreferences", "1"),
        requestTypePreferences=kwargs.get("requestTypePreferences", "2,3"),
        regionPreferences=kwargs.get("regionPreferences", None),
        address=kwargs.get("address", "Line address"),
        state=kwargs.get("state", "Sikkim"),
        bankAccountHolderName=kwargs.get("bankAccountHolderName", None),
        bankAccountNo=kwargs.get("bankAccountNo", None),
        bankIFSC=kwargs.get("bankIFSC", None),
        bankName=kwargs.get("bankName", None),
        imageAadhar=kwargs.get("imageAadhar", "https://example.com/aadhaar.png"),
        imagePAN=kwargs.get("imagePAN", "https://example.com/pan.png"),
        imageBankAccount=kwargs.get(
            "imageBankAccount", "https://example.com/passbook.png"
        ),
        tableTimestamp=kwargs.get("tableTimestamp", None),
    )
    db.add(user)
    db.commit()
    return user


def _valid_put(**overrides):
    body = {
        "bankAccountHolderName": "Test Holder",
        "bankAccountNo": "123456789012",
        "bankIFSC": "SBIN0001234",
        "bankName": "State Bank of India",
    }
    body.update(overrides)
    return body



def _pr38_auth_user(user_app_id: str, *, uid: int = 1):
    """Test helper: AuthenticatedUser with phone business id (PR38)."""
    from app_v1.auth.deps import AuthenticatedUser
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"test-auth-subject-{user_app_id}",
        user_app_id=str(user_app_id),
        account_session_id="test-account-session",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )

@pytest.fixture
def db_session():
    engine, Session = _memory_db()
    db = Session()
    try:
        yield db, engine, Session
    finally:
        db.close()


def _assert_no_sensitive_bank_fields(payload: dict):
    forbidden_keys = {
        "BANK_AC_NO",
        "bankAccountNo",
        "imageBankAccount",
        "imageAadhar",
        "imagePAN",
        "userAppId",
        "alsoVendor",
        "vendorApproved",
        "lockApp",
        "fcmToken",
        "UID",
        "password",
    }
    assert forbidden_keys.isdisjoint(set(payload.keys()))
    blob = str(payload)
    assert "123456789012" not in blob
    assert "secret-fcm-token" not in blob
    assert "https://example.com/aadhaar.png" not in blob
    assert "https://example.com/pan.png" not in blob
    assert "https://example.com/passbook.png" not in blob


# --- Auth / ownership -------------------------------------------------------


def test_get_no_token_returns_401(db_session):
    _, engine, Session = db_session
    app = FastAPI()
    app.include_router(user_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    response = client.get("/getregisteredbankaccount")
    assert response.status_code in (401, 403)


def test_put_no_token_returns_401(db_session):
    _, engine, Session = db_session
    app = FastAPI()
    app.include_router(user_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    response = client.put("/updatevendorbankdetails", json=_valid_put())
    assert response.status_code in (401, 403)


def test_get_jwt_selects_own_row_without_user_app_id(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountNo="123456789012",
        bankAccountHolderName="Holder A",
        bankIFSC="SBIN0001234",
        bankName="SBI",
    )
    _add_user(
        db,
        user_app_id=OTHER_USER,
        uid=2,
        bankAccountNo="999988887777",
        bankAccountHolderName="Holder B",
        bankIFSC="HDFC0001234",
        bankName="HDFC",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getregisteredbankaccount")
    assert response.status_code == 200
    body = response.json()
    assert body["hasBankAccount"] is True
    assert body["accountHolderName"] == "Holder A"
    assert body["maskedAccountNumber"].endswith("9012")
    assert "999988887777" not in str(body)
    _assert_no_sensitive_bank_fields(body)


def test_legacy_mismatched_user_app_id_get_403(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1, bankAccountNo="123456789012")
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get(
        "/getregisteredbankaccount",
        params={"userAppId": OTHER_USER},
    )
    assert response.status_code == 403


def test_legacy_mismatched_user_app_id_put_403(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(userAppId=OTHER_USER),
    )
    assert response.status_code == 403


def test_user_a_cannot_read_user_b(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountNo="111122223333",
        bankAccountHolderName="A",
    )
    _add_user(
        db,
        user_app_id=OTHER_USER,
        uid=2,
        bankAccountNo="444455556666",
        bankAccountHolderName="B",
        bankIFSC="HDFC0009999",
        bankName="HDFC",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get(
        "/getregisteredbankaccount",
        params={"userAppId": OTHER_USER},
    )
    assert response.status_code == 403


def test_user_a_cannot_update_user_b(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    _add_user(
        db,
        user_app_id=OTHER_USER,
        uid=2,
        bankAccountNo="444455556666",
        bankAccountHolderName="B",
        bankIFSC="HDFC0009999",
        bankName="HDFC",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(userAppId=OTHER_USER, bankAccountHolderName="Hacked"),
    )
    assert response.status_code == 403
    db.expire_all()
    other = db.query(User).filter(User.userAppId == OTHER_USER).one()
    assert other.bankAccountHolderName == "B"


def test_missing_jwt_user_404(db_session):
    _, engine, Session = db_session
    client = _pr17_client(engine, Session, MISSING_USER)
    get_resp = client.get("/getregisteredbankaccount")
    assert get_resp.status_code == 404
    assert get_resp.json()["detail"] == "USER_NOT_FOUND"
    put_resp = client.put("/updatevendorbankdetails", json=_valid_put())
    assert put_resp.status_code == 404
    assert put_resp.json()["detail"] == "USER_NOT_FOUND"


# --- Eligibility ------------------------------------------------------------


def test_customer_only_vendor_not_eligible(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        alsoVendor=False,
        vendorApproved=False,
    )
    client = _pr17_client(engine, Session, CUSTOMER_ID)
    get_resp = client.get("/getregisteredbankaccount")
    assert get_resp.status_code == 403
    assert get_resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"
    put_resp = client.put("/updatevendorbankdetails", json=_valid_put())
    assert put_resp.status_code == 403
    assert put_resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_pending_vendor_not_eligible(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
        bankAccountNo="123456789012",
    )
    client = _pr17_client(engine, Session, PENDING_VENDOR)
    get_resp = client.get("/getregisteredbankaccount")
    assert get_resp.status_code == 403
    assert get_resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"
    put_resp = client.put("/updatevendorbankdetails", json=_valid_put())
    assert put_resp.status_code == 403
    assert put_resp.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_approved_unlocked_vendor_allowed(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    get_resp = client.get("/getregisteredbankaccount")
    assert get_resp.status_code == 200
    put_resp = client.put("/updatevendorbankdetails", json=_valid_put())
    assert put_resp.status_code == 200
    assert put_resp.json()["message"] == "UPDATED"


def test_lock_app_account_locked(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=LOCKED_USER,
        uid=1,
        lockApp=True,
        alsoVendor=True,
        vendorApproved=True,
    )
    client = _pr17_client(engine, Session, LOCKED_USER)
    get_resp = client.get("/getregisteredbankaccount")
    assert get_resp.status_code == 403
    assert get_resp.json()["detail"] == "ACCOUNT_LOCKED"
    put_resp = client.put("/updatevendorbankdetails", json=_valid_put())
    assert put_resp.status_code == 403
    assert put_resp.json()["detail"] == "ACCOUNT_LOCKED"


def test_approval_revoked_before_transaction_rejected(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    db.query(User).filter(User.userAppId == APPROVED_VENDOR).update(
        {User.vendorApproved: False}
    )
    db.commit()
    response = client.put("/updatevendorbankdetails", json=_valid_put())
    assert response.status_code == 403
    assert response.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_lock_applied_before_transaction_rejected(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    db.query(User).filter(User.userAppId == APPROVED_VENDOR).update(
        {User.lockApp: True}
    )
    db.commit()
    response = client.put("/updatevendorbankdetails", json=_valid_put())
    assert response.status_code == 403
    assert response.json()["detail"] == "ACCOUNT_LOCKED"


# --- GET contract -----------------------------------------------------------


def test_get_no_bank_values(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getregisteredbankaccount")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "hasBankAccount": False,
        "maskedAccountNumber": None,
        "accountHolderName": None,
        "bankIFSC": None,
        "bankName": None,
    }


def test_get_blank_legacy_account(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountNo="   ",
        bankAccountHolderName="Legacy",
        bankIFSC="SBIN0001234",
        bankName="SBI",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getregisteredbankaccount")
    assert response.status_code == 200
    body = response.json()
    assert body["hasBankAccount"] is False
    assert body["maskedAccountNumber"] is None


def test_get_existing_bank_masked(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountNo="123456789012",
        bankAccountHolderName="Holder",
        bankIFSC="SBIN0001234",
        bankName="SBI",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getregisteredbankaccount")
    assert response.status_code == 200
    body = response.json()
    assert body["hasBankAccount"] is True
    assert body["maskedAccountNumber"] == "XXXXXXXX9012"
    assert body["accountHolderName"] == "Holder"
    assert body["bankIFSC"] == "SBIN0001234"
    assert body["bankName"] == "SBI"
    assert "123456789012" not in response.text
    _assert_no_sensitive_bank_fields(body)


def test_get_short_malformed_account_masked_safely(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountNo="12",
        bankAccountHolderName="Holder",
        bankIFSC="SBIN0001234",
        bankName="SBI",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getregisteredbankaccount")
    assert response.status_code == 200
    body = response.json()
    assert body["hasBankAccount"] is True
    assert body["maskedAccountNumber"] == "XXXX"
    assert "12" not in body["maskedAccountNumber"] or body["maskedAccountNumber"] == "XXXX"


def test_get_schema_keys_only(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountNo="AB12-345678",
        bankAccountHolderName="Holder",
        bankIFSC="SBIN0001234",
        bankName="SBI",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    body = client.get("/getregisteredbankaccount").json()
    assert set(body.keys()) == {
        "hasBankAccount",
        "maskedAccountNumber",
        "accountHolderName",
        "bankIFSC",
        "bankName",
    }


# --- PUT validation ---------------------------------------------------------


def test_put_all_four_required(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    for missing in (
        "bankAccountHolderName",
        "bankAccountNo",
        "bankIFSC",
        "bankName",
    ):
        body = _valid_put()
        del body[missing]
        response = client.put("/updatevendorbankdetails", json=body)
        assert response.status_code == 422, missing


def test_put_empty_holder_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankAccountHolderName="   "),
    )
    assert response.status_code == 422


def test_put_empty_bank_name_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankName=""),
    )
    assert response.status_code == 422


def test_put_valid_account_with_hyphen(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankAccountNo="1234-567890"),
    )
    assert response.status_code == 200
    db.expire_all()
    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert user.bankAccountNo == "1234-567890"


def test_put_account_too_short(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankAccountNo="12345"),
    )
    assert response.status_code == 422
    assert "ERROR_INVALID_ACCOUNTNO" in response.text


def test_put_account_too_long(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankAccountNo="1" * 23),
    )
    assert response.status_code == 422
    assert "ERROR_INVALID_ACCOUNTNO" in response.text


def test_put_account_invalid_characters(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankAccountNo="12345@67890"),
    )
    assert response.status_code == 422
    assert "ERROR_INVALID_ACCOUNTNO" in response.text


def test_put_valid_ifsc_and_lowercase_normalized(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankIFSC="sbin0004321"),
    )
    assert response.status_code == 200
    db.expire_all()
    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert user.bankIFSC == "SBIN0004321"


def test_put_invalid_ifsc(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(bankIFSC="INVALID"),
    )
    assert response.status_code == 422
    assert "ERROR_INVALID_IFSC" in response.text


# --- Mutation / preservation ------------------------------------------------


def test_put_updates_four_fields_preserves_others(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountHolderName="Old",
        bankAccountNo="111111111111",
        bankIFSC="SBIN0001111",
        bankName="Old Bank",
        imageBankAccount="https://example.com/passbook.png",
        imageAadhar="https://example.com/aadhaar.png",
        imagePAN="https://example.com/pan.png",
        alsoVendor=True,
        vendorApproved=True,
        lockApp=False,
        joiningDate=date(2024, 1, 15),
        requestTypePreferences="2,3",
        fcmToken="secret-fcm-token-should-not-leak",
        rating="4.5",
        fullName="Keep Name",
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    before = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    response = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(
            bankAccountHolderName="New Holder",
            bankAccountNo="222233334444",
            bankIFSC="hdfc0002222",
            bankName="HDFC Bank",
        ),
    )
    assert response.status_code == 200
    assert response.json() == {"message": "UPDATED"}
    db.expire_all()
    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert user.bankAccountHolderName == "New Holder"
    assert user.bankAccountNo == "222233334444"
    assert user.bankIFSC == "HDFC0002222"
    assert user.bankName == "HDFC Bank"
    assert user.imageBankAccount == "https://example.com/passbook.png"
    assert user.imageAadhar == "https://example.com/aadhaar.png"
    assert user.imagePAN == "https://example.com/pan.png"
    assert user.alsoVendor is True
    assert user.vendorApproved is True
    assert user.lockApp is False
    assert user.joiningDate == date(2024, 1, 15)
    assert user.requestTypePreferences == "2,3"
    assert user.fcmToken == "secret-fcm-token-should-not-leak"
    assert user.rating == "4.5"
    assert user.fullName == "Keep Name"
    assert user.tableTimestamp is not None
    # Asia/Kolkata naive timestamp should be near "now"
    assert abs((user.tableTimestamp - before).total_seconds()) < 120


def test_put_same_value_replay_updated_no_churn(db_session):
    db, engine, Session = db_session
    ts = datetime(2024, 6, 1, 10, 0, 0)
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        bankAccountHolderName="Test Holder",
        bankAccountNo="123456789012",
        bankIFSC="SBIN0001234",
        bankName="State Bank of India",
        tableTimestamp=ts,
    )
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    response = client.put("/updatevendorbankdetails", json=_valid_put())
    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    db.expire_all()
    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert user.tableTimestamp == ts


def test_put_db_failure_safe_500(db_session):
    from fastapi import HTTPException
    from app_v1.schemas.user_table import UserBankDetailsUpdate
    from sqlalchemy.exc import SQLAlchemyError

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def with_for_update(self):
            return self

        def first(self):
            raise SQLAlchemyError("db down")

    class FakeDb:
        def __init__(self):
            self.rolled_back = False

        def query(self, *a, **k):
            return FakeQuery()

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            raise AssertionError("should not commit")

    fake_db = FakeDb()
    with pytest.raises(HTTPException) as exc:
        user_crud.update_vendor_bank_details(
            fake_db,
            UserBankDetailsUpdate(**_valid_put()),
            user_id=APPROVED_VENDOR,
        )
    assert exc.value.status_code == 500
    assert exc.value.detail == "Unable to update bank details"
    assert "SQLAlchemy" not in str(exc.value.detail)
    assert "db down" not in str(exc.value.detail)
    assert fake_db.rolled_back is True


def test_put_no_email_side_effect(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    with patch.object(user_crud, "send_email") as email_mock:
        response = client.put("/updatevendorbankdetails", json=_valid_put())
    assert response.status_code == 200
    email_mock.assert_not_called()


def test_account_deletion_race_404(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    db.query(User).filter(User.userAppId == APPROVED_VENDOR).delete()
    db.commit()
    response = client.put("/updatevendorbankdetails", json=_valid_put())
    assert response.status_code == 404
    assert response.json()["detail"] == "USER_NOT_FOUND"


def test_two_updates_no_partial_mixed_fields(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    r1 = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(
            bankAccountHolderName="First",
            bankAccountNo="111111111111",
            bankIFSC="SBIN0001111",
            bankName="Bank One",
        ),
    )
    r2 = client.put(
        "/updatevendorbankdetails",
        json=_valid_put(
            bankAccountHolderName="Second",
            bankAccountNo="222222222222",
            bankIFSC="HDFC0002222",
            bankName="Bank Two",
        ),
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    db.expire_all()
    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert user.bankAccountHolderName == "Second"
    assert user.bankAccountNo == "222222222222"
    assert user.bankIFSC == "HDFC0002222"
    assert user.bankName == "Bank Two"


def test_row_lock_used(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    from app_v1.schemas.user_table import UserBankDetailsUpdate
    from sqlalchemy.orm.query import Query

    called = {"for_update": False}
    original_with_for_update = Query.with_for_update

    def _spy_with_for_update(self, *args, **kwargs):
        called["for_update"] = True
        return original_with_for_update(self, *args, **kwargs)

    with patch.object(Query, "with_for_update", _spy_with_for_update):
        result = user_crud.update_vendor_bank_details(
            db,
            UserBankDetailsUpdate(**_valid_put()),
            user_id=APPROVED_VENDOR,
        )
    assert result.message == "UPDATED"
    assert called["for_update"] is True


# --- Logging / security -----------------------------------------------------


def test_sensitive_values_not_logged(db_session, caplog):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=1)
    client = _pr17_client(engine, Session, APPROVED_VENDOR)
    with caplog.at_level(logging.DEBUG):
        client.put(
            "/updatevendorbankdetails",
            json=_valid_put(
                bankAccountHolderName="Secret Holder",
                bankAccountNo="998877665544",
                bankIFSC="SBIN0009988",
            ),
        )
        client.get("/getregisteredbankaccount")
    joined = " ".join(r.message for r in caplog.records)
    assert "998877665544" not in joined
    assert "Secret Holder" not in joined
    assert "SBIN0009988" not in joined
    assert "unit-test-jwt-secret" not in joined
