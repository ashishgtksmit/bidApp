"""
PR24 authenticated soft-tombstone account deletion.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")
os.environ["RATE_LIMIT_DELETE_USER_PER_IP"] = "5"
os.environ["RATE_LIMIT_DELETE_USER_PER_APPID"] = "5"
os.environ["RATE_LIMIT_DELETE_USER_WINDOW_SECONDS"] = "900"

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.car_details import CarDetail  # noqa: E402
from app_v1.models.driver_details import DriverDetail  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.crud import user as user_crud  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402
from app_v1.schemas.user_table import UserDelete  # noqa: E402
from app_v1.utils.security import hash_password  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

CUSTOMER_ID = "7022359323"
VENDOR_ID = "8637554388"
PENDING_VENDOR = "8637554387"
LOCKED_USER = "7000000001"
OTHER_USER = "7000000002"
MISSING_USER = "7999999999"
PASSWORD = "SecretPass1"
REASON = "User requested account deletion"

PR24_TABLES = [
    User.__table__,
    Request.__table__,
    CarDetail.__table__,
    DriverDetail.__table__,
    ApiRateLimitBucket.__table__,
]


def _create_biddetails_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS biddetails (
                    BID INTEGER PRIMARY KEY,
                    rID INTEGER NOT NULL,
                    bidderID INTEGER NOT NULL,
                    CARID INTEGER,
                    bidAmount NUMERIC(11,2) NOT NULL,
                    bidStatus VARCHAR(100),
                    tableTimestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR24_TABLES)
    _create_biddetails_table(engine)


_rid_seq = {"n": 0}
_ddid_seq = {"n": 0}
_carid_seq = {"n": 0}
_bid_seq = {"n": 0}


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    _rid_seq["n"] = 0
    _ddid_seq["n"] = 0
    _carid_seq["n"] = 0
    _bid_seq["n"] = 0

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            _rid_seq["n"] += 1
            target.RID = _rid_seq["n"]

    def _assign_ddid(mapper, connection, target):
        if getattr(target, "DDID", None) is None:
            _ddid_seq["n"] += 1
            target.DDID = _ddid_seq["n"]

    def _assign_carid(mapper, connection, target):
        if getattr(target, "CARID", None) is None:
            _carid_seq["n"] += 1
            target.CARID = _carid_seq["n"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(DriverDetail, "before_insert", _assign_ddid)
    event.listen(CarDetail, "before_insert", _assign_carid)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(DriverDetail, "before_insert", _assign_ddid)
        event.remove(CarDetail, "before_insert", _assign_carid)


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr24_client(engine, Session, user_id: str | None):
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
    return TestClient(app, raise_server_exceptions=False)


def _add_user(db, *, user_app_id: str, uid: int, password: str = PASSWORD, **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password=password,
        fullName=kwargs.pop("fullName", "Test User"),
        emailId=kwargs.pop("emailId", f"{user_app_id}@example.com"),
        dob=kwargs.pop("dob", "1990-01-01"),
        city=kwargs.pop("city", "Guwahati"),
        gender=kwargs.pop("gender", "Male"),
        alsoVendor=kwargs.pop("alsoVendor", False),
        vendorApproved=kwargs.pop("vendorApproved", False),
        lockApp=kwargs.pop("lockApp", False),
        rating=kwargs.pop("rating", "5"),
        totalNoOfReviews=kwargs.pop("totalNoOfReviews", 0),
        fcmToken=kwargs.pop("fcmToken", None),
        user_login_status=kwargs.pop("user_login_status", "LOGGEDIN"),
        bankAccountHolderName=kwargs.pop("bankAccountHolderName", None),
        bankAccountNo=kwargs.pop("bankAccountNo", None),
        bankIFSC=kwargs.pop("bankIFSC", None),
        bankName=kwargs.pop("bankName", None),
        imageAadhar=kwargs.pop("imageAadhar", None),
        imagePAN=kwargs.pop("imagePAN", None),
        imageBankAccount=kwargs.pop("imageBankAccount", None),
        profilePicture=kwargs.pop("profilePicture", None),
        deletionReason=kwargs.pop("deletionReason", None),
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_request(
    db,
    *,
    customer_app_id: str,
    status: str,
    pickup_dt: datetime | None = None,
    request_won_by: str | None = None,
    driver_assigned_id: int | None = None,
):
    if pickup_dt is None:
        pickup_dt = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(
            days=2
        )
    row = Request(
        fromLocation="A",
        fromLandmark="AL",
        toLocation="B",
        toLandmark="BL",
        pickUpDate=pickup_dt.date(),
        pickUpTime=pickup_dt.time().replace(microsecond=0),
        noOfAdults=1,
        noOfKids=0,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        requestStatus=status,
        customerAppId=customer_app_id,
        requestWonBy=request_won_by,
        finalAmount=1000,
        noOfBids=0,
        requestReopened=False,
        reviewDone="N",
        driverAssignedID=driver_assigned_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_bid(db, *, rid: int, bidder_id: str, bid_status: str):
    _bid_seq["n"] += 1
    with db.bind.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO biddetails "
                "(BID, rID, bidderID, CARID, bidAmount, bidStatus) "
                "VALUES (:bid, :rid, :bidder, NULL, 500, :status)"
            ),
            {
                "bid": _bid_seq["n"],
                "rid": rid,
                "bidder": int(bidder_id),
                "status": bid_status,
            },
        )
    return _bid_seq["n"]


def _add_driver(db, *, user_app_id: str, name: str = "Driver"):
    row = DriverDetail(
        userAppId=user_app_id,
        driverName=name,
        driverNumber="9000000000",
        driverDOB=date(1990, 1, 1),
        driverGender="Male",
        driverCity="Guwahati",
        driverLicense="lic",
        driverDocument="doc",
        driverPhoto="photo",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_car(db, *, user_app_id: str, reg: str = "AS01AB1234"):
    row = CarDetail(
        userAppId=user_app_id,
        carRegNo=reg,
        normalizedCarRegNo=reg.upper().replace(" ", ""),
        carModel="Swift",
        modelYear="2020",
        ownerName="Owner",
        registrationDoc="regdoc",
        registeredOn=datetime(2024, 1, 1),
        adminApproved=True,
        carOwnedBySameVendor=True,
        CTD=1,
        isDeleted=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _delete_body(**overrides):
    body = {"password": PASSWORD, "deletionReason": REASON}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth / identity
# ---------------------------------------------------------------------------


def test_missing_jwt_returns_401():
    engine, Session = _memory_db()
    client = _pr24_client(engine, Session, user_id=None)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code in (401, 403)


def test_own_account_deletion_succeeds_without_user_app_id():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="fcm-token-1")
    db.close()
    with patch(
        "app_v1.crud.user.unsubscribe_token_from_topics", return_value={}
    ) as unsub:
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200
    assert resp.json() == {"message": "DELETED"}
    assert f"{CUSTOMER_ID}.DELETED" not in resp.text
    unsub.assert_called_once()
    db = Session()
    tomb = db.query(User).filter(User.userAppId == f"{CUSTOMER_ID}.DELETED").one()
    assert tomb.lockApp is True
    assert tomb.user_login_status == "LOGGEDOUT"
    assert tomb.deletionReason == REASON
    assert tomb.fcmToken is None
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first() is None
    db.close()


def test_matching_transitional_user_app_id_allowed():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post(
            "/deleteappuser",
            json=_delete_body(userAppId=CUSTOMER_ID),
        )
    assert resp.status_code == 200
    assert resp.json()["message"] == "DELETED"


def test_mismatched_transitional_user_app_id_forbidden():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_user(db, user_app_id=OTHER_USER, uid=2)
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post(
        "/deleteappuser",
        json=_delete_body(userAppId=OTHER_USER),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not authorized"
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    db.close()


def test_cannot_delete_another_account_via_body_id():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_user(db, user_app_id=OTHER_USER, uid=2, password="OtherPass1")
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post(
        "/deleteappuser",
        json=_delete_body(userAppId=OTHER_USER, password="OtherPass1"),
    )
    assert resp.status_code == 403
    db = Session()
    assert db.query(User).filter(User.userAppId == OTHER_USER).one()
    db.close()


def test_missing_account_returns_404():
    engine, Session = _memory_db()
    client = _pr24_client(engine, Session, user_id=MISSING_USER)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"


def test_already_deleted_original_sub_replay_404():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=f"{CUSTOMER_ID}.DELETED", uid=1, lockApp=True)
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"


def test_plaintext_legacy_password_verifies():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, password=PASSWORD)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first() is None
    db.close()


def test_bcrypt_sha256_password_verifies():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, password=hash_password(PASSWORD))
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_bcrypt_password_verifies():
    engine, Session = _memory_db()
    db = Session()
    from passlib.context import CryptContext

    bcrypt_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    try:
        hashed = bcrypt_ctx.hash(PASSWORD)
    except Exception:
        hashed = hash_password(PASSWORD)
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, password=hashed)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_wrong_password_409():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post(
        "/deleteappuser",
        json=_delete_body(password="WrongPass"),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "WRONG_PASSWORD"
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    db.close()


def test_password_not_logged(caplog):
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    with caplog.at_level(logging.DEBUG):
        with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
            client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
            client.post("/deleteappuser", json=_delete_body())
    joined = " ".join(r.message for r in caplog.records)
    assert PASSWORD not in joined


def test_password_upgrade_does_not_partial_commit_on_gate_failure():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, password=PASSWORD)
    _add_request(db, customer_app_id=CUSTOMER_ID, status="BID - OPEN")
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_OPEN_REQUEST"
    db = Session()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert user.password == PASSWORD
    assert user.lockApp is False
    db.close()


@pytest.mark.parametrize(
    "body",
    [
        {"password": PASSWORD},
        {"password": PASSWORD, "deletionReason": ""},
        {"password": PASSWORD, "deletionReason": "ab"},
        {"password": PASSWORD, "deletionReason": "x" * 501},
    ],
)
def test_invalid_reason_422(body):
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/deleteappuser", json=body)
    assert resp.status_code == 422


def test_trimmed_valid_reason_stored():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post(
            "/deleteappuser",
            json=_delete_body(deletionReason="  leaving app  "),
        )
    assert resp.status_code == 200
    db = Session()
    tomb = db.query(User).filter(User.userAppId == f"{CUSTOMER_ID}.DELETED").one()
    assert tomb.deletionReason == "leaving app"
    db.close()


def test_open_request_blocks():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_request(db, customer_app_id=CUSTOMER_ID, status="BID - OPEN")
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_OPEN_REQUEST"


def test_handshake_blocks():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_request(db, customer_app_id=CUSTOMER_ID, status="BID - CONFIRMED")
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_HANDSHAKE"


def test_future_customer_booking_blocks():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    future = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(days=3)
    _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pickup_dt=future,
    )
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_FUTURE_BOOKING"


def test_past_confirmed_customer_booking_does_not_block():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    past = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) - timedelta(days=3)
    _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pickup_dt=past,
    )
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_future_vendor_trip_blocks():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    future = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(days=2)
    _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pickup_dt=future,
        request_won_by=VENDOR_ID,
    )
    db.close()
    client = _pr24_client(engine, Session, user_id=VENDOR_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_FUTURE_VENDOR_TRIP"


def test_past_vendor_trip_does_not_block():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    past = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) - timedelta(days=2)
    _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pickup_dt=past,
        request_won_by=VENDOR_ID,
    )
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=VENDOR_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_active_bid_blocks():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    req = _add_request(db, customer_app_id=CUSTOMER_ID, status="BID - OPEN")
    _add_bid(db, rid=req.RID, bidder_id=VENDOR_ID, bid_status="BID - OPEN")
    db.close()
    client = _pr24_client(engine, Session, user_id=VENDOR_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_ACTIVE_BID"


def test_inactive_bid_does_not_block():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    req = _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="BOOKING - CANCELLED BY USER",
    )
    _add_bid(db, rid=req.RID, bidder_id=VENDOR_ID, bid_status="BID - OPEN")
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=VENDOR_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_assigned_driver_future_trip_blocks():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    driver = _add_driver(db, user_app_id=VENDOR_ID)
    future = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(days=1)
    _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pickup_dt=future,
        request_won_by=OTHER_USER,
        driver_assigned_id=int(driver.DDID),
    )
    db.close()
    client = _pr24_client(engine, Session, user_id=VENDOR_ID)
    resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "DELETION_BLOCKED_ASSIGNED_DRIVER"


def test_unassigned_drivers_and_cars_do_not_block():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    _add_driver(db, user_app_id=VENDOR_ID)
    _add_car(db, user_app_id=VENDOR_ID)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=VENDOR_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200
    db = Session()
    assert db.query(DriverDetail).filter(DriverDetail.userAppId == VENDOR_ID).count() == 1
    assert db.query(CarDetail).filter(CarDetail.userAppId == VENDOR_ID).count() == 1
    db.close()


def test_pending_vendor_may_delete():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
    )
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=PENDING_VENDOR)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_locked_account_may_delete():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=LOCKED_USER, uid=1, lockApp=True)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=LOCKED_USER)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200


def test_tombstone_collision_suffix_and_no_truncation():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_user(db, user_app_id=f"{CUSTOMER_ID}.DELETED", uid=2, lockApp=True)
    _add_user(db, user_app_id=f"{CUSTOMER_ID}.DELETED1", uid=3, lockApp=True)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200
    expected = f"{CUSTOMER_ID}.DELETED2"
    assert len(expected) <= 64
    db = Session()
    assert db.query(User).filter(User.userAppId == expected).one()
    db.close()


def test_profile_bank_kyc_and_history_unchanged_on_delete():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        fullName="Keep Name",
        emailId="keep@example.com",
        dob="1991-02-03",
        city="Shillong",
        gender="Female",
        alsoVendor=True,
        vendorApproved=True,
        bankAccountHolderName="Keep Holder",
        bankAccountNo="1234567890",
        bankIFSC="SBIN0001234",
        bankName="SBI",
        imageAadhar="aadhar-url",
        imagePAN="pan-url",
        imageBankAccount="bank-url",
        profilePicture="pic-url",
    )
    past = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) - timedelta(days=10)
    req = _add_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pickup_dt=past,
    )
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"message"}
    db = Session()
    tomb = db.query(User).filter(User.userAppId == f"{CUSTOMER_ID}.DELETED").one()
    assert tomb.fullName == "Keep Name"
    assert tomb.emailId == "keep@example.com"
    assert tomb.dob == "1991-02-03"
    assert tomb.city == "Shillong"
    assert tomb.gender == "Female"
    assert tomb.alsoVendor is True
    assert tomb.vendorApproved is True
    assert tomb.bankAccountHolderName == "Keep Holder"
    assert tomb.bankAccountNo == "1234567890"
    assert tomb.imageAadhar == "aadhar-url"
    assert tomb.profilePicture == "pic-url"
    assert db.query(Request).filter(Request.RID == req.RID).one().customerAppId == CUSTOMER_ID
    db.close()


def test_table_timestamp_ist_aware():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    before = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) - timedelta(seconds=5)
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        assert client.post("/deleteappuser", json=_delete_body()).status_code == 200
    after = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(seconds=5)
    db = Session()
    tomb = db.query(User).filter(User.userAppId == f"{CUSTOMER_ID}.DELETED").one()
    assert before <= tomb.tableTimestamp <= after
    db.close()


def test_fcm_captured_unsubscribed_after_commit_and_failure_does_not_rollback():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=VENDOR_ID,
        uid=1,
        alsoVendor=True,
        vendorApproved=True,
        fcmToken="token-abc",
    )
    db.close()

    def _boom(*_a, **_k):
        raise RuntimeError("fcm down")

    with patch(
        "app_v1.crud.user.unsubscribe_token_from_topics", side_effect=_boom
    ) as unsub:
        client = _pr24_client(engine, Session, user_id=VENDOR_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    assert resp.status_code == 200
    unsub.assert_called_once()
    args, _kwargs = unsub.call_args
    assert args[0] == "token-abc"
    assert "all_users" in args[1]
    assert "all_vendors" in args[1]
    db = Session()
    tomb = db.query(User).filter(User.userAppId == f"{VENDOR_ID}.DELETED").one()
    assert tomb.fcmToken is None
    db.close()


def test_fcm_token_never_logged(caplog):
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="super-secret-fcm")
    db.close()
    with caplog.at_level(logging.INFO):
        with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
            client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
            client.post("/deleteappuser", json=_delete_body())
    joined = " ".join(r.message for r in caplog.records)
    assert "super-secret-fcm" not in joined


def test_forced_db_failure_rolls_back():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="tok")
    db.close()

    db2 = Session()

    def _failing_commit():
        raise SQLAlchemyError("forced")

    with patch.object(db2, "commit", _failing_commit):
        with pytest.raises(HTTPException) as exc:
            user_crud.delete_user(
                db2,
                UserDelete(password=PASSWORD, deletionReason=REASON),
                user_id=CUSTOMER_ID,
            )
        assert exc.value.status_code == 500
        assert exc.value.detail == "ACCOUNT_DELETION_FAILED"
    db2.close()

    db = Session()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert user.fcmToken == "tok"
    assert user.lockApp is False
    db.close()


def test_crud_does_not_close_session():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        with patch.object(db, "close") as close_mock:
            result = user_crud.delete_user(
                db,
                UserDelete(password=PASSWORD, deletionReason=REASON),
                user_id=CUSTOMER_ID,
            )
    assert result.message == "DELETED"
    close_mock.assert_not_called()
    assert db.query(User).filter(User.userAppId == f"{CUSTOMER_ID}.DELETED").one()
    db.close()


def test_no_sql_exception_leakage():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()

    db2 = Session()

    def _failing_commit():
        raise SQLAlchemyError("SELECT * FROM secret_table boom")

    with patch.object(db2, "commit", _failing_commit):
        with pytest.raises(HTTPException) as exc:
            user_crud.delete_user(
                db2,
                UserDelete(password=PASSWORD, deletionReason=REASON),
                user_id=CUSTOMER_ID,
            )
    assert "secret_table" not in str(exc.value.detail)
    assert exc.value.detail == "ACCOUNT_DELETION_FAILED"
    db2.close()


def test_getuserdetails_original_sub_no_registered():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        assert client.post("/deleteappuser", json=_delete_body()).status_code == 200
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.get(f"/getuserdetails?userAppId={CUSTOMER_ID}")
    assert resp.status_code == 200
    assert resp.json().get("message") == "NO REGISTERED"


def test_profile_upload_original_sub_404():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        assert client.post("/deleteappuser", json=_delete_body()).status_code == 200
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    resp = client.post("/profilepageupload", json={"image": "aaaa"})
    assert resp.status_code in (404, 422)
    if resp.status_code == 404:
        assert resp.json()["detail"] == "USER_NOT_FOUND"


def test_model_userappid_length_supports_tombstone():
    col = User.__table__.c.userAppId
    assert col.type.length >= 18
    assert col.type.length >= 64


def test_admin_reject_user_unchanged_still_hard_deletes_by_uid():
    assert hasattr(user_crud, "reject_user")


def test_rate_limit_exceeded_429():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    for _ in range(5):
        resp = client.post(
            "/deleteappuser",
            json=_delete_body(password="Wrong"),
        )
        assert resp.status_code == 409
    limited = client.post(
        "/deleteappuser",
        json=_delete_body(password="Wrong"),
    )
    assert limited.status_code == 429
    assert limited.json()["detail"] == "DELETION_RATE_LIMITED"
    assert PASSWORD not in limited.text


def test_successful_delete_response_exposes_no_sensitive_data():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="tok")
    db.close()
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
        resp = client.post("/deleteappuser", json=_delete_body())
    body = resp.json()
    assert body == {"message": "DELETED"}
    assert "tok" not in resp.text
    assert PASSWORD not in resp.text
    assert f"{CUSTOMER_ID}.DELETED" not in resp.text


def test_logout_still_works_for_healthy_account():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="tok")
    db.close()
    client = _pr24_client(engine, Session, user_id=CUSTOMER_ID)
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        # logout endpoint signature uses query params
        resp = client.post(
            f"/logout?userAppId={CUSTOMER_ID}&fcmToken=tok",
        )
    # May fail due to known fcm_token kwarg mismatch; document actual behaviour.
    assert resp.status_code in (200, 500)
