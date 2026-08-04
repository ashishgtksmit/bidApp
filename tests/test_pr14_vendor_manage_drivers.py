"""
PR14 vendor Manage Drivers — management list, OTP, create/update/delete.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import base64
import os
import sys
import types
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")
os.environ.setdefault("OTP_PEPPER", "unit-test-otp-pepper")
os.environ["OTP_TEST_BYPASS_SMS"] = "1"
os.environ["OTP_TEST_FIXED_OTP"] = "1234"
os.environ.setdefault("DRIVER_OTP_EXPIRY_MINUTES", "5")
os.environ.setdefault("DRIVER_OTP_MAX_ATTEMPTS", "5")
os.environ.setdefault("DRIVER_OTP_TOKEN_EXPIRY_MINUTES", "10")
# Generous rate limits for unit tests
os.environ["RATE_LIMIT_DRIVER_OTP_SEND_IP"] = "1000"
os.environ["RATE_LIMIT_DRIVER_OTP_SEND_USER"] = "1000"
os.environ["RATE_LIMIT_DRIVER_OTP_VERIFY_IP"] = "1000"
os.environ["RATE_LIMIT_DRIVER_OTP_VERIFY_USER"] = "1000"

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.driver_details import DriverDetail  # noqa: E402
from app_v1.models.driver_otp import DriverOtpChallenge, DriverOtpToken  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.models.tags_table import Tag  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.crud import driver as driver_crud  # noqa: E402
from app_v1.crud import driver_manage as manage_crud  # noqa: E402
from app_v1.schemas.driver_details import (  # noqa: E402
    CreateDriverDetail,
    DeleteDriverDetail,
    UpdateDriverDetail,
)
from app_v1.endpoints.driver import router as driver_router  # noqa: E402
from app_v1.utils import driver_otp as driver_otp_utils  # noqa: E402
from app_v1.utils.otp import hash_otp  # noqa: E402

CUSTOMER_ID = "7022359323"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
NON_VENDOR = "7000000001"

# Minimal 1x1 PNG
_PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")

PR14_TABLES = [
    User.__table__,
    Request.__table__,
    DriverDetail.__table__,
    DriverOtpChallenge.__table__,
    DriverOtpToken.__table__,
    ApiRateLimitBucket.__table__,
    Tag.__table__,
    LocationDetail.__table__,
]


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR14_TABLES)



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

@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    req_counter = {"n": 0}
    driver_counter = {"n": 0}

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    def _assign_ddid(mapper, connection, target):
        if getattr(target, "DDID", None) is None:
            driver_counter["n"] += 1
            target.DDID = driver_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(DriverDetail, "before_insert", _assign_ddid)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(DriverDetail, "before_insert", _assign_ddid)


def _add_user(db, *, user_app_id: str, uid: int, full_name: str = "User", **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="1000000000",
        fullName=full_name,
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
        joiningDate=kwargs.get("joiningDate", date(2020, 5, 1)),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 12),
        user_login_status="LOGGEDOUT",
        cityPreferences=kwargs.get("cityPreferences", "1"),
        requestTypePreferences=kwargs.get("requestTypePreferences", "1"),
        regionPreferences=kwargs.get("regionPreferences", None),
        bankAccountNo=kwargs.get("bankAccountNo", "SECRET-BANK"),
    )
    db.add(user)
    db.commit()
    return user


def _seed_driver(
    db,
    *,
    user_app_id: str,
    name: str = "Driver One",
    number: str = "9800000001",
    photo: str | None = "photo.jpg",
    city: str = "Gangtok",
    ts: datetime | None = None,
) -> DriverDetail:
    row = DriverDetail(
        userAppId=user_app_id,
        driverName=name,
        driverNumber=number,
        driverDOB=date(1990, 1, 1),
        driverGender="M",
        driverCity=city,
        driverLicense="SECRET-LICENSE-URL",
        driverDocument="SECRET-DOCUMENT-URL",
        driverPhoto=photo if photo is not None else "",
        tableTimestamp=ts or datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "REQUEST - CONFIRMED",
    request_won_by=VENDOR_A,
    driver_assigned_id: int | None = None,
) -> Request:
    row = Request(
        fromLocation="Gangtok",
        fromLandmark="MG Marg",
        toLocation="Siliguri",
        toLandmark="NJP",
        pickUpDate=date(2030, 8, 15),
        pickUpTime=time(10, 30),
        noOfAdults=2,
        noOfKids=1,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest="Window seat",
        bidEndTime=datetime(2030, 8, 14, 18, 0, 0),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=1,
        noOfBids=1,
        finalAmount=2500,
        WIZZPNR="WIZZ123",
        paymentStatus="PENDING",
        requestWonBy=request_won_by,
        rejectionReason=None,
        requestReopened=False,
        driverAssignedID=driver_assigned_id,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr14_client(engine, Session, user_id: str):
    app = FastAPI()
    app.include_router(driver_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app)


def _issue_create_token(db, vendor_id: str, phone: str) -> str:
    send = driver_otp_utils.send_driver_otp(
        db,
        vendor_app_id=vendor_id,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
    )
    assert send.message == "OTP_SENT"
    verified = driver_otp_utils.verify_driver_otp(
        db,
        vendor_app_id=vendor_id,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
        otp="1234",
    )
    assert isinstance(verified, dict)
    return verified["driverOtpToken"]


def _issue_change_token(db, vendor_id: str, phone: str, driver_id: int) -> str:
    send = driver_otp_utils.send_driver_otp(
        db,
        vendor_app_id=vendor_id,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CHANGE_DRIVER_PHONE,
        driver_id=driver_id,
    )
    assert send.message == "OTP_SENT"
    verified = driver_otp_utils.verify_driver_otp(
        db,
        vendor_app_id=vendor_id,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CHANGE_DRIVER_PHONE,
        otp="1234",
        driver_id=driver_id,
    )
    assert isinstance(verified, dict)
    return verified["driverOtpToken"]


# ---------------------------------------------------------------------------
# Management list
# ---------------------------------------------------------------------------


def test_management_list_own_active_drivers_safe_fields():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    d1 = _seed_driver(
        db,
        user_app_id=VENDOR_A,
        name="Alpha",
        number="9800000001",
        ts=datetime(2026, 2, 1, 10, 0, 0),
    )
    d1_id = d1.DDID
    d2 = _seed_driver(
        db,
        user_app_id=VENDOR_A,
        name="Beta",
        number="9800000002",
        photo=None,
        ts=datetime(2026, 3, 1, 10, 0, 0),
    )
    d2_id = d2.DDID
    _seed_driver(db, user_app_id=VENDOR_B, name="Other", number="9800000099")
    soft = _seed_driver(db, user_app_id=VENDOR_A, name="Gone", number="9800000003")
    soft.userAppId = "123456789"
    db.commit()
    db.close()

    client = _pr14_client(engine, Session, VENDOR_A)
    resp = client.get("/viewmanageddriversforvendor")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    # newest first
    assert data[0]["DRIVERNAME"] == "Beta"
    assert data[1]["DRIVERNAME"] == "Alpha"
    for row in data:
        assert set(row.keys()) <= {
            "DRIVERID",
            "DRIVERNAME",
            "DRIVERNUMBER",
            "DRIVERDOB",
            "GENDER",
            "DRIVERCITY",
            "PHOTO_URL",
            "ADDEDON",
        }
        assert "USERAPPID" not in row
        assert "LICENSE_URL" not in row
        assert "DOCUMENT_URL" not in row
        assert "FCMTOKEN" not in row
        assert "SECRET" not in str(row)
    assert data[0]["PHOTO_URL"] is None
    assert data[1]["DRIVERID"] == d1_id
    assert data[0]["DRIVERID"] == d2_id


def test_management_list_empty_and_non_vendor():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(
        db,
        user_app_id=NON_VENDOR,
        uid=2,
        alsoVendor=False,
        vendorApproved=False,
    )
    db.close()

    client = _pr14_client(engine, Session, VENDOR_A)
    resp = client.get("/viewmanageddriversforvendor")
    assert resp.status_code == 200
    assert resp.json() == []

    client_nv = _pr14_client(engine, Session, NON_VENDOR)
    resp2 = client_nv.get("/viewmanageddriversforvendor")
    assert resp2.status_code == 403


def test_pr13_lean_route_unchanged_excludes_kyc():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_driver(db, user_app_id=VENDOR_A)
    db.close()

    client = _pr14_client(engine, Session, VENDOR_A)
    resp = client.get("/viewdriversforvendor")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert set(data[0].keys()) == {
        "DRIVERID",
        "DRIVERNAME",
        "PHOTO_URL",
        "DRIVERNUMBER",
    }
    assert "DRIVERDOB" not in data[0]
    assert "LICENSE_URL" not in data[0]
    assert "USERAPPID" not in data[0]


def test_management_list_sql_error_safe():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    db.close()

    with patch.object(
        manage_crud,
        "require_active_vendor",
        side_effect=SQLAlchemyError("boom"),
    ):
        result = manage_crud.get_managed_drivers_for_vendor(
            Session(), user_id=VENDOR_A
        )
    assert hasattr(result, "message")
    assert result.message == "ERROR"
    assert "boom" not in str(result.message)


# ---------------------------------------------------------------------------
# Driver OTP
# ---------------------------------------------------------------------------


def test_driver_otp_send_verify_create_purpose_hash_not_plaintext():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    phone = "9876543210"

    result = driver_otp_utils.send_driver_otp(
        db,
        vendor_app_id=VENDOR_A,
        driver_phone=f" {phone} ",
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
    )
    assert result.message == "OTP_SENT"

    challenge = db.query(DriverOtpChallenge).first()
    assert challenge is not None
    assert challenge.driver_phone == phone
    assert challenge.otp_hash == hash_otp("1234")
    assert challenge.otp_hash != "1234"
    assert challenge.vendor_app_id == VENDOR_A
    assert challenge.purpose == "CREATE_DRIVER"

    verified = driver_otp_utils.verify_driver_otp(
        db,
        vendor_app_id=VENDOR_A,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
        otp="1234",
    )
    assert verified["message"] == "OTP_VERIFIED"
    assert verified["driverOtpToken"]
    assert "reset_token" not in verified
    # challenge removed
    assert db.query(DriverOtpChallenge).count() == 0
    token_row = db.query(DriverOtpToken).first()
    assert token_row is not None
    assert token_row.used is False
    assert token_row.token_hash != verified["driverOtpToken"]


def test_driver_otp_wrong_otp_expired_attempts_isolation():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    phone = "9876543210"
    driver_otp_utils.send_driver_otp(
        db,
        vendor_app_id=VENDOR_A,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
    )
    bad = driver_otp_utils.verify_driver_otp(
        db,
        vendor_app_id=VENDOR_A,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
        otp="0000",
    )
    assert bad.message == "ERROR_INVALID_OTP"

    # Wrong vendor cannot verify
    bad_v = driver_otp_utils.verify_driver_otp(
        db,
        vendor_app_id=VENDOR_B,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
        otp="1234",
    )
    assert bad_v.message == "ERROR_INVALID_OTP"

    # Expire
    ch = db.query(DriverOtpChallenge).first()
    ch.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=1
    )
    db.commit()
    expired = driver_otp_utils.verify_driver_otp(
        db,
        vendor_app_id=VENDOR_A,
        driver_phone=phone,
        purpose=driver_otp_utils.PURPOSE_CREATE_DRIVER,
        otp="1234",
    )
    assert expired.message == "ERROR_OTP_EXPIRED"


def test_driver_otp_change_requires_owned_driver_http():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    own = _seed_driver(db, user_app_id=VENDOR_A, number="9800000001")
    other = _seed_driver(db, user_app_id=VENDOR_B, number="9800000002")
    own_id = own.DDID
    other_id = other.DDID
    db.close()

    client = _pr14_client(engine, Session, VENDOR_A)
    ok = client.post(
        "/driverotp/send",
        json={
            "driverPhone": "9800111222",
            "purpose": "CHANGE_DRIVER_PHONE",
            "driverId": own_id,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["message"] == "OTP_SENT"
    assert "otp" not in {k.lower() for k in ok.json().keys() if k != "message"}

    forbidden = client.post(
        "/driverotp/send",
        json={
            "driverPhone": "9800111222",
            "purpose": "CHANGE_DRIVER_PHONE",
            "driverId": other_id,
        },
    )
    assert forbidden.status_code == 403

    missing = client.post(
        "/driverotp/send",
        json={
            "driverPhone": "9800111222",
            "purpose": "CHANGE_DRIVER_PHONE",
            "driverId": 99999,
        },
    )
    assert missing.status_code == 404


def test_create_otp_token_cannot_authorize_phone_change():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    driver = _seed_driver(db, user_app_id=VENDOR_A, number="9800000001")
    create_token = _issue_create_token(db, VENDOR_A, "9800111222")

    result = driver_otp_utils.validate_driver_otp_token(
        db,
        raw_token=create_token,
        vendor_app_id=VENDOR_A,
        driver_phone="9800111222",
        purpose=driver_otp_utils.PURPOSE_CHANGE_DRIVER_PHONE,
        driver_id=driver.DDID,
    )
    assert hasattr(result, "message")
    assert result.message == "ERROR_INVALID_OTP_TOKEN"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_driver_jwt_owner_duplicate_and_otp():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    phone = "9876500001"
    token = _issue_create_token(db, VENDOR_A, phone)

    with patch.object(
        manage_crud,
        "azure_blob_upload",
        side_effect=lambda **kwargs: (True, f"https://blob.example/{kwargs['blob_name']}.jpg"),
    ), patch.object(manage_crud, "send_email", return_value={"message": "SENT"}):
        body = CreateDriverDetail(
            driverName="New Driver",
            driverNumber=phone,
            driverDOB=date(1992, 5, 5),
            driverGender="Male",
            driverCity="Gangtok",
            driverLicenseImg=_PNG_B64,
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token,
            userAppId="SHOULD_BE_IGNORED",
        )
        result = manage_crud.insert_driver_for_vendor(db, body, VENDOR_A)
        assert result.message == "INSERTED"

    row = (
        db.query(DriverDetail)
        .filter(DriverDetail.driverNumber == phone)
        .first()
    )
    assert row is not None
    assert row.userAppId == VENDOR_A
    assert row.driverGender == "M"
    # token consumed
    tok = db.query(DriverOtpToken).first()
    assert tok.used is True

    # Same vendor duplicate
    token2 = _issue_create_token(db, VENDOR_A, phone)
    with patch.object(
        manage_crud,
        "azure_blob_upload",
        side_effect=lambda **kwargs: (True, f"https://blob.example/{kwargs['blob_name']}.jpg"),
    ):
        body2 = CreateDriverDetail(
            driverName="Dup",
            driverNumber=phone,
            driverDOB=date(1992, 5, 5),
            driverGender="M",
            driverCity="Gangtok",
            driverLicenseImg=_PNG_B64,
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token2,
        )
        with pytest.raises(HTTPException) as exc:
            manage_crud.insert_driver_for_vendor(db, body2, VENDOR_A)
        assert exc.value.status_code == 409
        assert exc.value.detail == "ERROR_ALREADY_EXISTS"

    # Different vendor same phone allowed
    token3 = _issue_create_token(db, VENDOR_B, phone)
    with patch.object(
        manage_crud,
        "azure_blob_upload",
        side_effect=lambda **kwargs: (True, f"https://blob.example/b/{kwargs['blob_name']}.jpg"),
    ), patch.object(manage_crud, "send_email", return_value={"message": "SENT"}):
        body3 = CreateDriverDetail(
            driverName="Other Vendor Driver",
            driverNumber=phone,
            driverDOB=date(1992, 5, 5),
            driverGender="F",
            driverCity="Siliguri",
            driverLicenseImg=_PNG_B64,
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token3,
        )
        result3 = manage_crud.insert_driver_for_vendor(db, body3, VENDOR_B)
        assert result3.message == "INSERTED"


def test_create_rejects_consumed_token_and_future_dob():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    phone = "9876500002"
    token = _issue_create_token(db, VENDOR_A, phone)
    # consume manually
    row = db.query(DriverOtpToken).first()
    row.used = True
    db.commit()

    with patch.object(
        manage_crud,
        "azure_blob_upload",
        side_effect=lambda **kwargs: (True, "https://blob.example/x.jpg"),
    ):
        body = CreateDriverDetail(
            driverName="X",
            driverNumber=phone,
            driverDOB=date(1990, 1, 1),
            driverGender="M",
            driverCity="Gangtok",
            driverLicenseImg=_PNG_B64,
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token,
        )
        with pytest.raises(HTTPException) as exc:
            manage_crud.insert_driver_for_vendor(db, body, VENDOR_A)
        assert exc.value.status_code == 422

    phone2 = "9876500003"
    token2 = _issue_create_token(db, VENDOR_A, phone2)
    future = date.today() + timedelta(days=30)
    with patch.object(
        manage_crud,
        "azure_blob_upload",
        side_effect=lambda **kwargs: (True, "https://blob.example/y.jpg"),
    ):
        body2 = CreateDriverDetail(
            driverName="Future",
            driverNumber=phone2,
            driverDOB=future,
            driverGender="M",
            driverCity="Gangtok",
            driverLicenseImg=_PNG_B64,
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token2,
        )
        with pytest.raises(HTTPException) as exc2:
            manage_crud.insert_driver_for_vendor(db, body2, VENDOR_A)
        assert exc2.value.detail == "ERROR_INVALID_DOB"


def test_create_media_failure_no_partial_row_token_not_consumed():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    phone = "9876500004"
    token = _issue_create_token(db, VENDOR_A, phone)

    with patch.object(
        manage_crud,
        "azure_blob_upload",
        return_value=(False, "INVALID_BASE64"),
    ):
        body = CreateDriverDetail(
            driverName="Fail Media",
            driverNumber=phone,
            driverDOB=date(1990, 1, 1),
            driverGender="M",
            driverCity="Gangtok",
            driverLicenseImg="not-valid",
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token,
        )
        with pytest.raises(HTTPException):
            manage_crud.insert_driver_for_vendor(db, body, VENDOR_A)

    assert db.query(DriverDetail).count() == 0
    tok = db.query(DriverOtpToken).first()
    assert tok.used is False


def test_soft_deleted_phone_can_be_recreated():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    phone = "9876500005"
    old = _seed_driver(db, user_app_id=VENDOR_A, number=phone)
    old.userAppId = "123456789"
    db.commit()
    token = _issue_create_token(db, VENDOR_A, phone)

    with patch.object(
        manage_crud,
        "azure_blob_upload",
        side_effect=lambda **kwargs: (True, f"https://blob.example/{kwargs['blob_name']}.jpg"),
    ), patch.object(manage_crud, "send_email", return_value={"message": "SENT"}):
        body = CreateDriverDetail(
            driverName="Recreated",
            driverNumber=phone,
            driverDOB=date(1991, 2, 2),
            driverGender="F",
            driverCity="Gangtok",
            driverLicenseImg=_PNG_B64,
            driverDocumentImg=_PNG_B64,
            driverPhotoImg=_PNG_B64,
            driverOtpToken=token,
        )
        result = manage_crud.insert_driver_for_vendor(db, body, VENDOR_A)
        assert result.message == "INSERTED"

    active = (
        db.query(DriverDetail)
        .filter(
            DriverDetail.userAppId == VENDOR_A,
            DriverDetail.driverNumber == phone,
        )
        .all()
    )
    assert len(active) == 1


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_city_no_otp_phone_change_requires_token():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    driver = _seed_driver(
        db, user_app_id=VENDOR_A, number="9800000100", city="Gangtok"
    )
    other = _seed_driver(db, user_app_id=VENDOR_B, number="9800000101")

    # city-only
    body = UpdateDriverDetail(
        DRIVERID=driver.DDID,
        driverCity="Siliguri",
        driverNumber="9800000100",
    )
    result = manage_crud.update_driver_for_vendor(db, body, VENDOR_A)
    assert result.message == "UPDATED"
    db.refresh(driver)
    assert driver.driverCity == "Siliguri"
    assert driver.driverName == "Driver One"  # immutable

    # phone change without token
    body2 = UpdateDriverDetail(
        DRIVERID=driver.DDID,
        driverCity="Siliguri",
        driverNumber="9800000999",
    )
    with pytest.raises(HTTPException) as exc:
        manage_crud.update_driver_for_vendor(db, body2, VENDOR_A)
    assert exc.value.detail == "ERROR_OTP_TOKEN_REQUIRED"

    # phone change with token
    token = _issue_change_token(db, VENDOR_A, "9800000999", driver.DDID)
    body3 = UpdateDriverDetail(
        DRIVERID=driver.DDID,
        driverCity="Siliguri",
        driverNumber="9800000999",
        driverOtpToken=token,
    )
    result3 = manage_crud.update_driver_for_vendor(db, body3, VENDOR_A)
    assert result3.message == "UPDATED"
    db.refresh(driver)
    assert driver.driverNumber == "9800000999"

    # wrong owner
    body4 = UpdateDriverDetail(
        DRIVERID=other.DDID,
        driverCity="X",
        driverNumber="9800000101",
    )
    with pytest.raises(HTTPException) as exc2:
        manage_crud.update_driver_for_vendor(db, body4, VENDOR_A)
    assert exc2.value.status_code == 403


def test_update_same_value_idempotent_and_missing():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    driver = _seed_driver(db, user_app_id=VENDOR_A, number="9800000200", city="Gangtok")
    old_ts = driver.tableTimestamp

    body = UpdateDriverDetail(
        DRIVERID=driver.DDID,
        driverCity="Gangtok",
        driverNumber="9800000200",
    )
    result = manage_crud.update_driver_for_vendor(db, body, VENDOR_A)
    assert result.message == "UPDATED"
    db.refresh(driver)
    assert driver.tableTimestamp == old_ts

    with pytest.raises(HTTPException) as exc:
        manage_crud.update_driver_for_vendor(
            db,
            UpdateDriverDetail(
                DRIVERID=99999,
                driverCity="X",
                driverNumber="9800000200",
            ),
            VENDOR_A,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_soft_delete_active_trip_block_and_replay():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    _add_user(db, user_app_id=CUSTOMER_ID, uid=3, alsoVendor=False)
    free = _seed_driver(db, user_app_id=VENDOR_A, number="9800000300")
    assigned = _seed_driver(db, user_app_id=VENDOR_A, number="9800000301")
    other = _seed_driver(db, user_app_id=VENDOR_B, number="9800000302")
    _seed_request(
        db,
        status="REQUEST - CONFIRMED",
        driver_assigned_id=assigned.DDID,
    )
    _seed_request(
        db,
        status="REQUEST - CANCELLED",
        driver_assigned_id=free.DDID,
    )

    # active trip blocks
    with pytest.raises(HTTPException) as exc:
        manage_crud.delete_driver_for_vendor(
            db, DeleteDriverDetail(driverId=assigned.DDID), VENDOR_A
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "DRIVER_ASSIGNED_TO_ACTIVE_TRIP"
    db.refresh(assigned)
    assert assigned.userAppId == VENDOR_A

    # cancelled reference does not block
    result = manage_crud.delete_driver_for_vendor(
        db, DeleteDriverDetail(driverId=free.DDID), VENDOR_A
    )
    assert result.message == "DELETED"
    db.refresh(free)
    assert free.userAppId == "123456789"
    assert free.driverLicense == "SECRET-LICENSE-URL"  # media retained
    assert free.DDID == free.DDID

    # disappears from management + lean lists
    managed = manage_crud.get_managed_drivers_for_vendor(db, VENDOR_A)
    assert all(d.DRIVERID != free.DDID for d in managed)
    lean = driver_crud.get_all_driver_for_vendor(db, VENDOR_A)
    assert all(d.DRIVERID != free.DDID for d in lean)

    # replay → 404
    with pytest.raises(HTTPException) as exc2:
        manage_crud.delete_driver_for_vendor(
            db, DeleteDriverDetail(driverId=free.DDID), VENDOR_A
        )
    assert exc2.value.status_code == 404

    # wrong owner → 403
    with pytest.raises(HTTPException) as exc3:
        manage_crud.delete_driver_for_vendor(
            db, DeleteDriverDetail(driverId=other.DDID), VENDOR_A
        )
    assert exc3.value.status_code == 403


def test_http_create_update_delete_smoke():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    phone = "9876511111"
    token = _issue_create_token(db, VENDOR_A, phone)
    db.close()

    client = _pr14_client(engine, Session, VENDOR_A)
    with patch(
        "app_v1.crud.driver_manage.azure_blob_upload",
        side_effect=lambda **kwargs: (True, f"https://blob.example/{kwargs['blob_name']}.jpg"),
    ), patch(
        "app_v1.crud.driver_manage.send_email",
        return_value={"message": "SENT"},
    ), patch(
        "app_v1.crud.driver_manage.azure_blob_delete_by_url",
        return_value=True,
    ):
        create = client.post(
            "/insertnewdriver",
            json={
                "driverName": "HTTP Driver",
                "driverNumber": phone,
                "driverDOB": "1990-01-01",
                "driverGender": "Male",
                "driverCity": "Gangtok",
                "driverLicenseImg": _PNG_B64,
                "driverDocumentImg": _PNG_B64,
                "driverPhotoImg": _PNG_B64,
                "driverOtpToken": token,
                "userAppId": "ignored",
            },
        )
        assert create.status_code == 200
        assert create.json()["message"] == "INSERTED"

        listed = client.get("/viewmanageddriversforvendor")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        driver_id = listed.json()[0]["DRIVERID"]

        update = client.post(
            "/updatedriverdetails",
            json={
                "DRIVERID": driver_id,
                "driverCity": "Pelling",
                "driverNumber": phone,
                "driverPhotoImg": None,
            },
        )
        assert update.status_code == 200
        assert update.json()["message"] == "UPDATED"

        delete = client.put(
            "/deletedriverfromprofile",
            json={"driverId": driver_id},
        )
        assert delete.status_code == 200
        assert delete.json()["message"] == "DELETED"

        listed2 = client.get("/viewmanageddriversforvendor")
        assert listed2.json() == []
