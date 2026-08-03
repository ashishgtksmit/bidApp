"""
PR20 booking history — customer completed + vendor confirmed FastAPI endpoints.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")
os.environ.setdefault("DB_PASSWORD", "unused")
os.environ.setdefault("DB_USERNAME", "unused")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "3306")
os.environ.setdefault("DB_NAME", "unused")

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
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.models.car_details import CarDetail  # noqa: E402
from app_v1.models.driver_details import DriverDetail  # noqa: E402
from app_v1.models.customer_reviews import CustomerReview  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.crud import request as request_crud  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

CUSTOMER_ID = "7022359323"
OTHER_CUSTOMER = "7000000003"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
LOSING_BIDDER = "8637554399"

SECRET_FCM = "secret-fcm-token-should-not-leak"
SECRET_BANK = "SECRET-BANK-ACCOUNT-1234"
SECRET_PASSWORD = "secret-password-should-not-leak"
SECRET_PHONE = "9000000000"
SECRET_LICENSE = "secret-license-doc"

PR20_CORE_TABLES = [
    User.__table__,
    Request.__table__,
    DriverDetail.__table__,
    CustomerReview.__table__,
]

CUSTOMER_FORBIDDEN_KEYS = {
    "customerAppId",
    "CUSTOMERAPPID",
    "requestWonBy",
    "REQUESTWONBY",
    "driverNumber",
    "DRIVERNUMBER",
    "driverLicense",
    "DRIVERLICENSE",
    "registrationDoc",
    "REGISTRATIONDOC",
    "powerOfAttorneyDoc",
    "POWEROFATTORNEYDOC",
    "fcmToken",
    "email",
    "emailId",
    "bankAccountNo",
}

VENDOR_FORBIDDEN_KEYS = {
    "customerAppId",
    "CUSTOMERAPPID",
    "vendorId",
    "PHONENUMBER",
    "ALTNUMBER",
    "alternateNumber",
    "fcmToken",
    "email",
    "emailId",
    "bankAccountNo",
    "driverLicense",
    "DRIVERLICENSE",
    "registrationDoc",
    "REGISTRATIONDOC",
    "powerOfAttorneyDoc",
    "POWEROFATTORNEYDOC",
}


# ---------------------------------------------------------------------------
# Engine / schema bootstrap
# ---------------------------------------------------------------------------


def _create_extra_sqlite(engine) -> None:
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
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS cardetails (
                    CARID INTEGER PRIMARY KEY,
                    userAppId VARCHAR(10) NOT NULL,
                    carRegNo VARCHAR(100) NOT NULL,
                    normalizedCarRegNo VARCHAR(100) NOT NULL DEFAULT '',
                    carColor VARCHAR(200),
                    carModel VARCHAR(200) NOT NULL,
                    modelYear VARCHAR(10) NOT NULL,
                    ownerName VARCHAR(300) NOT NULL,
                    registrationDoc TEXT NOT NULL,
                    powerOfAttorneyDoc TEXT,
                    registeredOn TIMESTAMP NOT NULL,
                    adminApproved BOOLEAN NOT NULL,
                    carOwnedBySameVendor BOOLEAN NOT NULL,
                    CTD INTEGER NOT NULL,
                    imageVehicleFront TEXT,
                    imageVehicleSide TEXT,
                    isDeleted BOOLEAN NOT NULL DEFAULT 0,
                    deletedAt TIMESTAMP,
                    deletedBy VARCHAR(10)
                )
                """
            )
        )


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR20_CORE_TABLES)
    _create_extra_sqlite(engine)


def _memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    return engine


_bid_id_seq = {"n": 0}
_car_id_seq = {"n": 100}
_driver_id_seq = {"n": 0}


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    req_counter = {"n": 0}
    cr_counter = {"n": 0}
    _bid_id_seq["n"] = 0
    _car_id_seq["n"] = 100
    _driver_id_seq["n"] = 0

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    def _assign_cr(mapper, connection, target):
        if getattr(target, "CR", None) is None:
            cr_counter["n"] += 1
            target.CR = cr_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(CustomerReview, "before_insert", _assign_cr)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(CustomerReview, "before_insert", _assign_cr)


@pytest.fixture
def db_session():
    engine = _memory_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db, engine, Session
    finally:
        db.close()


@pytest.fixture
def seeded_db(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, full_name="Customer One")
    _add_user(db, user_app_id=VENDOR_A, uid=2, full_name="Vendor A")
    _add_user(db, user_app_id=OTHER_CUSTOMER, uid=3, full_name="Customer Two")
    _add_user(db, user_app_id=VENDOR_B, uid=4, full_name="Vendor B")
    _add_user(db, user_app_id=LOSING_BIDDER, uid=5, full_name="Losing Bidder")
    return db, engine, Session


def _client_for(engine, Session, user_id):
    app = FastAPI()
    app.include_router(request_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    if user_id is not None:
        app.dependency_overrides[get_current_user_id] = lambda: user_id
    return TestClient(app)


def _add_user(db, *, user_app_id: str, uid: int, full_name: str = "User", **kwargs) -> User:
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password=kwargs.get("password", SECRET_PASSWORD),
        alternateNumber="1000000000",
        fullName=full_name,
        emailId=f"{user_app_id}@example.com",
        dob="1990-01-01",
        city=kwargs.get("city", "Gangtok"),
        gender=kwargs.get("gender", "Male"),
        profilePicture=kwargs.get("profilePicture", "images/profilepic_male.png"),
        alsoVendor=kwargs.get("alsoVendor", True),
        vendorApproved=kwargs.get("vendorApproved", True),
        lockApp=kwargs.get("lockApp", False),
        customerRating=kwargs.get("customerRating", "4.50"),
        totalCustomerReviews=kwargs.get("totalCustomerReviews", 0),
        rating=kwargs.get("rating", "4.50"),
        totalNoOfReviews=kwargs.get("totalNoOfReviews", 0),
        fcmToken=kwargs.get("fcmToken", SECRET_FCM),
        joiningDate=kwargs.get("joiningDate", date(2024, 1, 15)),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 5),
        user_login_status="LOGGEDOUT",
        bankAccountNo=kwargs.get("bankAccountNo", SECRET_BANK),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _yesterday_pickup():
    now_ist = datetime.now(IST)
    yesterday = (now_ist - timedelta(days=1)).date()
    return yesterday, time(9, 0, 0)


def _days_ago_pickup(days: int, hour: int = 9):
    now_ist = datetime.now(IST)
    d = (now_ist - timedelta(days=days)).date()
    return d, time(hour, 0, 0)


def _tomorrow_pickup():
    now_ist = datetime.now(IST)
    tomorrow = (now_ist + timedelta(days=1)).date()
    return tomorrow, time(9, 0, 0)


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    vendor_app_id: str | None = VENDOR_A,
    status: str = "REQUEST - CONFIRMED",
    pickup_date=None,
    pickup_time=None,
    review_done: str = "N",
    customer_review_done: str = "N",
    from_location: str = "Gangtok",
    to_location: str = "Siliguri",
    driver_assigned_id: int | None = None,
    final_amount: int = 4500,
    special_request: str | None = "",
) -> Request:
    if pickup_date is None or pickup_time is None:
        pd, pt = _yesterday_pickup()
    else:
        pd, pt = pickup_date, pickup_time
    row = Request(
        fromLocation=from_location,
        fromLandmark="MG Marg",
        toLocation=to_location,
        toLandmark="NJP",
        pickUpDate=pd,
        pickUpTime=pt,
        noOfAdults=2,
        noOfKids=0,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest=special_request,
        requestStatus=status,
        customerAppId=customer_app_id,
        requestWonBy=vendor_app_id,
        finalAmount=final_amount,
        noOfBids=1,
        reviewDone=review_done,
        customerReviewDone=customer_review_done,
        driverAssignedID=driver_assigned_id,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_car(db, *, user_app_id: str, reg: str = "SK01A1111", model: str = "Swift") -> int:
    _car_id_seq["n"] += 1
    car_id = _car_id_seq["n"]
    db.execute(
        text(
            """
            INSERT INTO cardetails
                (CARID, userAppId, carRegNo, normalizedCarRegNo, carColor, carModel, modelYear, ownerName,
                 registrationDoc, powerOfAttorneyDoc, registeredOn, adminApproved,
                 carOwnedBySameVendor, CTD, imageVehicleFront, imageVehicleSide,
                 isDeleted, deletedAt, deletedBy)
            VALUES
                (:car, :uid, :reg, :norm, 'White', :model, '2022', 'Owner',
                 'SECRET-REG-DOC', 'SECRET-POA', :ts, 1, 1, 1, 'front.png', 'side.png',
                 0, NULL, NULL)
            """
        ),
        {
            "car": car_id,
            "uid": user_app_id,
            "reg": reg,
            "norm": reg.replace(" ", "").upper(),
            "model": model,
            "ts": "2026-01-01 12:00:00",
        },
    )
    db.commit()
    return car_id


def _seed_bid(db, *, rid: int, bidder_id: str, amount: float = 4500.0, car_id: int | None = None) -> None:
    _bid_id_seq["n"] += 1
    bid_id = _bid_id_seq["n"]
    db.execute(
        text(
            """
            INSERT INTO biddetails
                (BID, rID, bidderID, CARID, bidAmount, bidStatus, tableTimestamp, last_updated)
            VALUES
                (:bid, :rid, :bidder, :car, :amount, :status, :ts, :ts)
            """
        ),
        {
            "bid": bid_id,
            "rid": rid,
            "bidder": int(bidder_id),
            "car": car_id,
            "amount": amount,
            "status": "REQUEST - CONFIRMED",
            "ts": "2026-01-01 12:00:00",
        },
    )
    db.commit()


def _seed_driver(db, *, user_app_id: str, driver_name: str = "Driver Name") -> int:
    _driver_id_seq["n"] += 1
    ddid = _driver_id_seq["n"]
    row = DriverDetail(
        DDID=ddid,
        userAppId=user_app_id,
        driverName=driver_name,
        driverNumber=SECRET_PHONE,
        driverDOB=date(1990, 1, 1),
        driverGender="Male",
        driverCity="Gangtok",
        driverLicense=SECRET_LICENSE,
        driverDocument="secret-driver-doc",
        driverPhoto="https://example.com/driver.png",
    )
    db.add(row)
    db.commit()
    return ddid


def _seed_customer_review(
    db,
    *,
    rid,
    giver_app_id: str,
    receiver_app_id: str,
    general_rating="4.5",
    comments: str = "Polite, On time",
) -> CustomerReview:
    row = CustomerReview(
        RID=str(rid),
        ratingGiverUserAppId=giver_app_id,
        ratingReceiverUserAppId=receiver_app_id,
        generalRating=Decimal(general_rating),
        comments=comments,
        tableTimestamp=datetime(2026, 1, 1, 10, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_customer_history_without_jwt_401(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, user_id=None)
    resp = client.get("/getallrequestforuser")
    assert resp.status_code in (401, 403)


def test_vendor_history_without_jwt_401(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, user_id=None)
    resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Customer history
# ---------------------------------------------------------------------------


def test_customer_gets_own_past_confirmed(seeded_db):
    db, engine, Session = seeded_db
    ddid = _seed_driver(db, user_app_id=VENDOR_A, driver_name="Ramesh")
    car_id = _seed_car(db, user_app_id=VENDOR_A)
    req = _seed_request(db, driver_assigned_id=ddid, review_done="N")
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, car_id=car_id)

    client = _client_for(engine, Session, CUSTOMER_ID)
    resp = client.get("/getallrequestforuser")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert item["requestId"] == req.RID
    assert item["requestStatus"] == "REQUEST - CONFIRMED"
    assert item["fromLocation"] == "Gangtok"
    assert item["toLocation"] == "Siliguri"
    assert item["reviewDone"] is False
    assert item["driverName"] == "Ramesh"
    assert item["driverProfileImageUrl"] == "https://example.com/driver.png"
    assert item["driverGender"] == "Male"
    assert item["driverDateOfBirth"] == "1990-01-01"
    assert item["carRegistrationNumber"] == "SK01A1111"
    assert item["carModel"] == "Swift"
    assert item["modelYear"] == 2022
    assert item["acRequested"] is True
    assert item["carrierRequested"] is False
    assert item["pickupDate"]
    assert item["pickupTime"]
    for key in CUSTOMER_FORBIDDEN_KEYS:
        assert key not in item
    assert SECRET_PHONE not in str(body)
    assert SECRET_LICENSE not in str(body)
    assert "SECRET-REG-DOC" not in str(body)
    assert CUSTOMER_ID not in str(body) or "requestId" in item


def test_customer_excludes_other_customer_trips(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, customer_app_id=OTHER_CUSTOMER, vendor_app_id=VENDOR_A)
    _seed_request(db, customer_app_id=CUSTOMER_ID, vendor_app_id=VENDOR_A)

    client = _client_for(engine, Session, CUSTOMER_ID)
    resp = client.get("/getallrequestforuser")
    assert resp.status_code == 200
    ids = {row["requestId"] for row in resp.json()}
    assert len(ids) == 1


def test_customer_no_customer_app_id_required(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    resp = client.get("/getallrequestforuser")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_customer_mismatched_transitional_customer_app_id_403(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    resp = client.get(
        "/getallrequestforuser",
        params={"customerAppId": OTHER_CUSTOMER},
    )
    assert resp.status_code == 403


def test_customer_excludes_future_open_cancelled(seeded_db):
    db, engine, Session = seeded_db
    past_d, past_t = _yesterday_pickup()
    future_d, future_t = _tomorrow_pickup()
    keep = _seed_request(db, pickup_date=past_d, pickup_time=past_t)
    _seed_request(
        db,
        pickup_date=future_d,
        pickup_time=future_t,
        from_location="Future",
    )
    _seed_request(
        db,
        status="BID - OPEN",
        pickup_date=past_d,
        pickup_time=past_t,
        from_location="Open",
    )
    _seed_request(
        db,
        status="BOOKING - CANCELLED BY USER",
        pickup_date=past_d,
        pickup_time=past_t,
        from_location="Cancelled",
    )

    client = _client_for(engine, Session, CUSTOMER_ID)
    body = client.get("/getallrequestforuser").json()
    assert len(body) == 1
    assert body[0]["requestId"] == keep.RID


def test_customer_reopened_original_excluded_clone_included_when_past_confirmed(
    seeded_db,
):
    db, engine, Session = seeded_db
    past_d, past_t = _yesterday_pickup()
    original = _seed_request(
        db,
        status="BOOKING - CANCELLED BY USER",
        pickup_date=past_d,
        pickup_time=past_t,
        from_location="Original",
    )
    original.requestReopened = True
    db.add(original)
    db.commit()

    clone = _seed_request(
        db,
        status="REQUEST - CONFIRMED",
        pickup_date=past_d,
        pickup_time=past_t,
        from_location="Clone",
    )

    client = _client_for(engine, Session, CUSTOMER_ID)
    body = client.get("/getallrequestforuser").json()
    ids = {row["requestId"] for row in body}
    assert original.RID not in ids
    assert clone.RID in ids


def test_customer_empty_list(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, CUSTOMER_ID)
    resp = client.get("/getallrequestforuser")
    assert resp.status_code == 200
    assert resp.json() == []


def test_customer_newest_pickup_first_with_rid_tiebreak(seeded_db):
    db, engine, Session = seeded_db
    older_d, older_t = _days_ago_pickup(5, hour=8)
    newer_d, newer_t = _days_ago_pickup(1, hour=10)
    same_d, same_t = _days_ago_pickup(2, hour=12)

    r_old = _seed_request(
        db, pickup_date=older_d, pickup_time=older_t, from_location="Old"
    )
    r_same_a = _seed_request(
        db, pickup_date=same_d, pickup_time=same_t, from_location="SameA"
    )
    r_same_b = _seed_request(
        db, pickup_date=same_d, pickup_time=same_t, from_location="SameB"
    )
    r_new = _seed_request(
        db, pickup_date=newer_d, pickup_time=newer_t, from_location="New"
    )

    client = _client_for(engine, Session, CUSTOMER_ID)
    body = client.get("/getallrequestforuser").json()
    ids = [row["requestId"] for row in body]
    assert ids[0] == r_new.RID
    # Same pickup: higher RID first
    same_ids = [i for i in ids if i in (r_same_a.RID, r_same_b.RID)]
    assert same_ids == sorted(same_ids, reverse=True)
    assert ids[-1] == r_old.RID


def test_customer_review_done_mapping(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, review_done="Y")
    client = _client_for(engine, Session, CUSTOMER_ID)
    body = client.get("/getallrequestforuser").json()
    assert body[0]["reviewDone"] is True


def test_customer_nullable_driver_car(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, driver_assigned_id=None)
    client = _client_for(engine, Session, CUSTOMER_ID)
    item = client.get("/getallrequestforuser").json()[0]
    assert item["driverName"] is None
    assert item["driverProfileImageUrl"] is None
    assert item["carRegistrationNumber"] is None
    assert item["carModel"] is None
    assert item["modelYear"] is None


def test_customer_malformed_pickup_history_data_invalid(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)

    def _boom(request_row):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )

    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch.object(request_crud, "_history_pickup_datetime", side_effect=_boom):
        resp = client.get("/getallrequestforuser")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "HISTORY_DATA_INVALID"
    assert "sql" not in str(resp.json()).lower()
    assert "traceback" not in str(resp.json()).lower()
    _ = req  # seeded


# ---------------------------------------------------------------------------
# Vendor history
# ---------------------------------------------------------------------------


def test_vendor_gets_own_past_confirmed(seeded_db):
    db, engine, Session = seeded_db
    ddid = _seed_driver(db, user_app_id=VENDOR_A, driver_name="Sita")
    car_id = _seed_car(db, user_app_id=VENDOR_A, reg="SK02B2222", model="Innova")
    req = _seed_request(
        db,
        vendor_app_id=VENDOR_A,
        driver_assigned_id=ddid,
        customer_review_done="Y",
        final_amount=4500,
    )
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, car_id=car_id)
    _seed_customer_review(
        db,
        rid=req.RID,
        giver_app_id=VENDOR_A,
        receiver_app_id=CUSTOMER_ID,
        general_rating="4.5",
        comments="Polite, On time",
    )

    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["requestId"] == req.RID
    assert item["finalAmount"] == 4500.0
    assert item["customerDisplayName"] == "Customer One"
    assert item["customerProfileImageUrl"] == "images/profilepic_male.png"
    assert item["customerReviewDone"] is True
    assert item["customerGeneralRating"] == 4.5
    assert item["customerReviewComments"] == "Polite, On time"
    assert item["carRegistrationNumber"] == "SK02B2222"
    assert item["carModel"] == "Innova"
    assert item["driverName"] == "Sita"
    for key in VENDOR_FORBIDDEN_KEYS:
        assert key not in item
    assert SECRET_FCM not in str(body)
    assert SECRET_BANK not in str(body)
    assert CUSTOMER_ID not in str(body) or "requestId" in item
    assert "1000000000" not in str(body)


def test_losing_bidder_gets_no_row(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, vendor_app_id=VENDOR_A)
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A)
    _seed_bid(db, rid=req.RID, bidder_id=LOSING_BIDDER)

    client = _client_for(engine, Session, LOSING_BIDDER)
    resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code == 200
    assert resp.json() == []


def test_vendor_excludes_other_winner(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_B)
    _seed_request(db, vendor_app_id=VENDOR_A)

    client = _client_for(engine, Session, VENDOR_A)
    body = client.get("/getallconfirmedrequestsforvendor").json()
    assert len(body) == 1


def test_vendor_no_vendor_id_required(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_vendor_mismatched_transitional_vendor_id_403(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get(
        "/getallconfirmedrequestsforvendor",
        params={"vendorId": VENDOR_B},
    )
    assert resp.status_code == 403


def test_vendor_excludes_future_and_cancelled(seeded_db):
    db, engine, Session = seeded_db
    past_d, past_t = _yesterday_pickup()
    future_d, future_t = _tomorrow_pickup()
    keep = _seed_request(
        db, vendor_app_id=VENDOR_A, pickup_date=past_d, pickup_time=past_t
    )
    _seed_request(
        db,
        vendor_app_id=VENDOR_A,
        pickup_date=future_d,
        pickup_time=future_t,
        from_location="Future",
    )
    _seed_request(
        db,
        vendor_app_id=VENDOR_A,
        status="BOOKING - CANCELLED BY USER",
        pickup_date=past_d,
        pickup_time=past_t,
        from_location="Cancelled",
    )

    client = _client_for(engine, Session, VENDOR_A)
    body = client.get("/getallconfirmedrequestsforvendor").json()
    assert len(body) == 1
    assert body[0]["requestId"] == keep.RID


def test_vendor_empty_list(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code == 200
    assert resp.json() == []


def test_vendor_newest_pickup_first(seeded_db):
    db, engine, Session = seeded_db
    older_d, older_t = _days_ago_pickup(4)
    newer_d, newer_t = _days_ago_pickup(1)
    r_old = _seed_request(
        db, vendor_app_id=VENDOR_A, pickup_date=older_d, pickup_time=older_t
    )
    r_new = _seed_request(
        db, vendor_app_id=VENDOR_A, pickup_date=newer_d, pickup_time=newer_t
    )
    client = _client_for(engine, Session, VENDOR_A)
    ids = [row["requestId"] for row in client.get("/getallconfirmedrequestsforvendor").json()]
    assert ids[0] == r_new.RID
    assert ids[-1] == r_old.RID


def test_vendor_nullable_car_driver(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A, driver_assigned_id=None)
    item = _client_for(engine, Session, VENDOR_A).get(
        "/getallconfirmedrequestsforvendor"
    ).json()[0]
    assert item["carRegistrationNumber"] is None
    assert item["carModel"] is None
    assert item["driverName"] is None
    assert item["customerReviewDone"] is False
    assert item["customerGeneralRating"] is None


def test_vendor_customer_review_done_false_without_rating(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A, customer_review_done="N")
    item = _client_for(engine, Session, VENDOR_A).get(
        "/getallconfirmedrequestsforvendor"
    ).json()[0]
    assert item["customerReviewDone"] is False


def test_vendor_malformed_pickup_history_data_invalid(seeded_db):
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)

    def _boom(request_row):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )

    client = _client_for(engine, Session, VENDOR_A)
    with patch.object(request_crud, "_history_pickup_datetime", side_effect=_boom):
        resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "HISTORY_DATA_INVALID"
    assert "sqlalchemy" not in str(resp.json()).lower()


def test_vendor_locked_still_sees_history(seeded_db):
    """Past history remains available even if vendor is later locked."""
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    user = db.query(User).filter(User.userAppId == VENDOR_A).one()
    user.lockApp = True
    user.vendorApproved = False
    db.add(user)
    db.commit()

    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get("/getallconfirmedrequestsforvendor")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
