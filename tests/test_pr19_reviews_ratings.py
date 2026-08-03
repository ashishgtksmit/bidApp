"""
PR19 review/rating — vendor and customer feedback FastAPI endpoints.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import importlib.util
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
from app_v1.models.vendor_reviews import VendorReview  # noqa: E402
from app_v1.models.customer_reviews import CustomerReview  # noqa: E402
from app_v1.endpoints.review import router as review_router  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

CUSTOMER_ID = "7022359323"
VENDOR_ID = "8637554388"
OTHER_CUSTOMER = "7000000003"
OTHER_VENDOR = "7000000002"
MISSING_VENDOR = "8600000099"
MISSING_CUSTOMER = "7099999999"

SECRET_FCM = "secret-fcm-token-should-not-leak"
SECRET_BANK = "SECRET-BANK-ACCOUNT-1234"
SECRET_PASSWORD = "secret-password-should-not-leak"

PR19_CORE_TABLES = [
    User.__table__,
    Request.__table__,
    DriverDetail.__table__,
    VendorReview.__table__,
    CustomerReview.__table__,
]


# ---------------------------------------------------------------------------
# Engine / schema bootstrap
# ---------------------------------------------------------------------------


def _create_extra_sqlite(engine) -> None:
    """biddetails / cardetails carry FK strings that reference mixed-case table
    names that don't match the lowercase __tablename__ values, so Base.metadata
    .create_all() cannot resolve them under SQLite. Create these tables with raw
    DDL instead (mirrors tests/test_pr12_customer_booking_cancellation.py)."""
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
    Base.metadata.create_all(bind=engine, tables=PR19_CORE_TABLES)
    _create_extra_sqlite(engine)


def _memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    return engine


# BigInteger primary keys don't get SQLite's ROWID autoincrement alias
# treatment, so RID / VRID / CR must be assigned manually before insert.
_bid_id_seq = {"n": 0}
_car_id_seq = {"n": 100}
_driver_id_seq = {"n": 0}


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    req_counter = {"n": 0}
    vr_counter = {"n": 0}
    cr_counter = {"n": 0}
    _bid_id_seq["n"] = 0
    _car_id_seq["n"] = 100
    _driver_id_seq["n"] = 0

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    def _assign_vrid(mapper, connection, target):
        if getattr(target, "VRID", None) is None:
            vr_counter["n"] += 1
            target.VRID = vr_counter["n"]

    def _assign_cr(mapper, connection, target):
        if getattr(target, "CR", None) is None:
            cr_counter["n"] += 1
            target.CR = cr_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(VendorReview, "before_insert", _assign_vrid)
    event.listen(CustomerReview, "before_insert", _assign_cr)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(VendorReview, "before_insert", _assign_vrid)
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
    _add_user(db, user_app_id=VENDOR_ID, uid=2, full_name="Vendor One")
    _add_user(db, user_app_id=OTHER_CUSTOMER, uid=3, full_name="Customer Two")
    _add_user(db, user_app_id=OTHER_VENDOR, uid=4, full_name="Vendor Two")
    return db, engine, Session


def _client_for(engine, Session, user_id):
    app = FastAPI()
    app.include_router(review_router)

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


def _fresh_session(Session):
    return Session()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


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
    return yesterday, time(9, 0)


def _tomorrow_pickup():
    now_ist = datetime.now(IST)
    tomorrow = (now_ist + timedelta(days=1)).date()
    return tomorrow, time(9, 0)


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    vendor_app_id: str | None = VENDOR_ID,
    status: str = "REQUEST - CONFIRMED",
    pickup_date=None,
    pickup_time=None,
    review_done: str = "N",
    customer_review_done: str = "N",
    from_location: str = "Gangtok",
    to_location: str = "Siliguri",
    driver_assigned_id: int | None = None,
    final_amount: int = 2500,
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
        specialRequest="",
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


def _seed_vendor_review(
    db,
    *,
    rid,
    vendor_app_id: str,
    customer_app_id: str = CUSTOMER_ID,
    driver_behaviour="4.5",
    punctuality="4.0",
    car_condition="3.5",
    cleanliness="5.0",
    comments: str = "Great ride",
    ts=None,
) -> VendorReview:
    row = VendorReview(
        customerAppId=customer_app_id,
        RID=rid,
        VENDORID=int(vendor_app_id),
        driverBehaviour=Decimal(driver_behaviour),
        punctuality=Decimal(punctuality),
        carCondition=Decimal(car_condition),
        cleanliness=Decimal(cleanliness),
        refreshments=False,
        comments=comments,
        tableTimestamp=ts or datetime(2026, 1, 1, 10, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_customer_review(
    db,
    *,
    rid,
    giver_app_id: str,
    receiver_app_id: str,
    general_rating="4.5",
    comments: str = "Great passenger",
    ts=None,
) -> CustomerReview:
    row = CustomerReview(
        RID=str(rid),
        ratingGiverUserAppId=giver_app_id,
        ratingReceiverUserAppId=receiver_app_id,
        generalRating=Decimal(general_rating),
        comments=comments,
        tableTimestamp=ts or datetime(2026, 1, 1, 10, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_bid(db, *, rid: int, bidder_id: str, amount: float, status: str = "REQUEST - CONFIRMED", car_id: int | None = None) -> None:
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
            "status": status,
            "ts": "2026-01-01 12:00:00",
        },
    )
    db.commit()


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
                (:car, :uid, :reg, :norm, 'White', :model, '2020', 'Owner',
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


def _seed_driver(db, *, user_app_id: str, driver_name: str = "Driver Name") -> int:
    _driver_id_seq["n"] += 1
    ddid = _driver_id_seq["n"]
    row = DriverDetail(
        DDID=ddid,
        userAppId=user_app_id,
        driverName=driver_name,
        driverNumber="9000000000",
        driverDOB=date(1990, 1, 1),
        driverGender="Male",
        driverCity="Gangtok",
        driverLicense="secret-license-doc",
        driverDocument="secret-driver-doc",
        driverPhoto="secret-driver-photo",
    )
    db.add(row)
    db.commit()
    return ddid


def _get_user(Session, user_app_id: str) -> User:
    s = _fresh_session(Session)
    try:
        return s.query(User).filter(User.userAppId == user_app_id).one()
    finally:
        s.close()


def _get_request(Session, rid) -> Request:
    s = _fresh_session(Session)
    try:
        return s.query(Request).filter(Request.RID == rid).one()
    finally:
        s.close()


def _count_vendor_reviews(Session, rid=None) -> int:
    s = _fresh_session(Session)
    try:
        q = s.query(VendorReview)
        if rid is not None:
            q = q.filter(VendorReview.RID == rid)
        return q.count()
    finally:
        s.close()


def _count_customer_reviews(Session, rid=None) -> int:
    s = _fresh_session(Session)
    try:
        q = s.query(CustomerReview)
        if rid is not None:
            q = q.filter(CustomerReview.RID == str(rid))
        return q.count()
    finally:
        s.close()


VENDOR_FEEDBACK_BODY = {
    "driverBehaviour": 4.5,
    "punctuality": 4.0,
    "carCondition": 3.5,
    "cleanliness": 5.0,
    "comments": "Great trip",
}


def _vendor_feedback_body(rid, **overrides):
    body = {"RID": rid, **VENDOR_FEEDBACK_BODY}
    body.update(overrides)
    return body


def _customer_feedback_body(rid, **overrides):
    body = {"RID": rid, "RATING": 4.5, "COMMENTS": "Great passenger"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Auth — 401/403 without JWT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/getallreviewsforvendor?VENDORID=" + VENDOR_ID, None),
        ("get", "/getallreviewsforcustomer", None),
        ("post", "/insertfeedback", {"RID": 1, **VENDOR_FEEDBACK_BODY}),
        ("post", "/insertcustomerfeedback", {"RID": 1, "RATING": 4.5, "COMMENTS": ""}),
    ],
)
def test_routes_without_jwt_return_401(seeded_db, method, path, body):
    _, engine, Session = seeded_db
    app = FastAPI()
    app.include_router(review_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    if method == "get":
        response = client.get(path)
    else:
        response = client.post(path, json=body)
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Migration preflight helper (unit-style)
# ---------------------------------------------------------------------------

_PREFLIGHT_PATH = (
    ROOT
    / "migrations"
    / "pr19_reviews_ratings_decimal_unique_rid"
    / "preflight_numeric_ratings.py"
)

_is_valid_half_rating = None
if _PREFLIGHT_PATH.exists():
    _spec = importlib.util.spec_from_file_location(
        "pr19_preflight_numeric_ratings", _PREFLIGHT_PATH
    )
    _preflight_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_preflight_mod)
    _is_valid_half_rating = getattr(_preflight_mod, "_is_valid_half_rating", None)


@pytest.mark.skipif(_is_valid_half_rating is None, reason="preflight helper not importable")
@pytest.mark.parametrize(
    "value",
    ["0.5", "1.0", "2.5", "5.0", 0.5, 5.0, Decimal("3.5")],
)
def test_preflight_valid_half_ratings(value):
    assert _is_valid_half_rating(value) is True


@pytest.mark.skipif(_is_valid_half_rating is None, reason="preflight helper not importable")
@pytest.mark.parametrize(
    "value",
    [None, "0", "0.3", "5.5", "abc", "-1.0", 0, 5.5],
)
def test_preflight_invalid_half_ratings(value):
    assert _is_valid_half_rating(value) is False


# ---------------------------------------------------------------------------
# 2. GET /getallreviewsforvendor
# ---------------------------------------------------------------------------


def test_vendor_get_public_read_by_any_authenticated_user(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    _seed_vendor_review(db, rid=req.RID, vendor_app_id=VENDOR_ID)
    client = _client_for(engine, Session, OTHER_CUSTOMER)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_vendor_get_empty_list_when_no_reviews(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    assert response.status_code == 200
    assert response.json() == []


def test_vendor_get_newest_first_order(seeded_db):
    db, engine, Session = seeded_db
    req1 = _seed_request(db, from_location="Gangtok", to_location="Siliguri")
    req2 = _seed_request(db, from_location="Pelling", to_location="Darjeeling")
    _seed_vendor_review(db, rid=req1.RID, vendor_app_id=VENDOR_ID, comments="First")
    _seed_vendor_review(db, rid=req2.RID, vendor_app_id=VENDOR_ID, comments="Second")
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert payload[0]["comments"] == "Second"
    assert payload[1]["comments"] == "First"


def test_vendor_get_half_ratings_preserved(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    _seed_vendor_review(
        db,
        rid=req.RID,
        vendor_app_id=VENDOR_ID,
        driver_behaviour="0.5",
        punctuality="1.5",
        car_condition="2.5",
        cleanliness="4.5",
    )
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    assert response.status_code == 200
    row = response.json()[0]
    assert row["driverBehaviour"] == 0.5
    assert row["punctuality"] == 1.5
    assert row["carCondition"] == 2.5
    assert row["cleanliness"] == 4.5


def test_vendor_get_no_sensitive_fields_in_response(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    _seed_vendor_review(db, rid=req.RID, vendor_app_id=VENDOR_ID)
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    assert response.status_code == 200
    row = response.json()[0]
    assert "customerAppId" not in row
    assert "VENDORID" not in row
    assert "phone" not in row
    assert "fcmToken" not in row
    blob = str(response.json())
    assert CUSTOMER_ID not in blob
    assert VENDOR_ID not in blob
    assert SECRET_FCM not in blob
    assert SECRET_BANK not in blob
    assert SECRET_PASSWORD not in blob


def test_vendor_get_missing_vendor_404(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get(
        "/getallreviewsforvendor", params={"VENDORID": MISSING_VENDOR}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "TARGET_NOT_FOUND"


def test_vendor_get_non_numeric_vendor_id_but_registered_returns_empty(seeded_db):
    """A vendor user row exists but its userAppId can't map onto the BigInteger
    VENDORID column — the list must degrade to [] rather than error."""
    db, engine, Session = seeded_db
    _add_user(db, user_app_id="ABCDEFGHIJ", uid=99, full_name="Weird Vendor")
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get(
        "/getallreviewsforvendor", params={"VENDORID": "ABCDEFGHIJ"}
    )
    assert response.status_code == 200
    assert response.json() == []


def test_vendor_get_car_and_driver_join_populated(seeded_db):
    db, engine, Session = seeded_db
    driver_id = _seed_driver(db, user_app_id=VENDOR_ID, driver_name="Ramesh Driver")
    car_id = _seed_car(db, user_app_id=VENDOR_ID, reg="SK01Z9999", model="Innova")
    req = _seed_request(db, driver_assigned_id=driver_id)
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_ID, amount=2500, car_id=car_id)
    _seed_vendor_review(db, rid=req.RID, vendor_app_id=VENDOR_ID)
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    assert response.status_code == 200
    row = response.json()[0]
    assert row["carRegNo"] == "SK01Z9999"
    assert row["carModel"] == "Innova"
    assert row["driverName"] == "Ramesh Driver"
    assert row["fromLocation"] == "Gangtok"
    assert row["toLocation"] == "Siliguri"


# ---------------------------------------------------------------------------
# 3. GET /getallreviewsforcustomer
# ---------------------------------------------------------------------------


def test_customer_get_own_reviews_only(seeded_db):
    db, engine, Session = seeded_db
    req_a = _seed_request(db, customer_app_id=CUSTOMER_ID)
    req_b = _seed_request(db, customer_app_id=OTHER_CUSTOMER)
    _seed_customer_review(
        db, rid=req_a.RID, giver_app_id=VENDOR_ID, receiver_app_id=CUSTOMER_ID
    )
    _seed_customer_review(
        db, rid=req_b.RID, giver_app_id=VENDOR_ID, receiver_app_id=OTHER_CUSTOMER
    )
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforcustomer")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["requestId"] == req_a.RID


def test_customer_get_ignores_customerid_query_param(seeded_db):
    db, engine, Session = seeded_db
    req_a = _seed_request(db, customer_app_id=CUSTOMER_ID)
    _seed_customer_review(
        db, rid=req_a.RID, giver_app_id=VENDOR_ID, receiver_app_id=CUSTOMER_ID
    )
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get(
        "/getallreviewsforcustomer", params={"CUSTOMERID": OTHER_CUSTOMER}
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["requestId"] == req_a.RID


def test_customer_get_empty_list_when_no_reviews(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforcustomer")
    assert response.status_code == 200
    assert response.json() == []


def test_customer_get_giver_join_shows_vendor_name(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, customer_app_id=CUSTOMER_ID)
    _seed_customer_review(
        db, rid=req.RID, giver_app_id=VENDOR_ID, receiver_app_id=CUSTOMER_ID
    )
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforcustomer")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["reviewerDisplayName"] == "Vendor One"


def test_customer_get_user_a_cannot_read_as_user_b(seeded_db):
    db, engine, Session = seeded_db
    req_a = _seed_request(db, customer_app_id=CUSTOMER_ID)
    _seed_customer_review(
        db, rid=req_a.RID, giver_app_id=VENDOR_ID, receiver_app_id=CUSTOMER_ID
    )
    client_b = _client_for(engine, Session, OTHER_CUSTOMER)
    response = client_b.get("/getallreviewsforcustomer")
    assert response.status_code == 200
    assert response.json() == []


def test_customer_get_no_sensitive_fields_in_response(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, customer_app_id=CUSTOMER_ID)
    _seed_customer_review(
        db, rid=req.RID, giver_app_id=VENDOR_ID, receiver_app_id=CUSTOMER_ID
    )
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforcustomer")
    assert response.status_code == 200
    row = response.json()[0]
    assert "ratingGiverUserAppId" not in row
    assert "ratingReceiverUserAppId" not in row
    assert "fcmToken" not in row
    blob = str(response.json())
    assert CUSTOMER_ID not in blob
    assert VENDOR_ID not in blob
    assert SECRET_FCM not in blob
    assert SECRET_BANK not in blob


# ---------------------------------------------------------------------------
# 4. POST /insertfeedback — customer rates vendor
# ---------------------------------------------------------------------------


def test_insert_vendor_feedback_success_201(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 201
    assert response.json() == {"message": "INSERTED"}


def test_insert_vendor_feedback_car_condition_persists_independently(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    body = _vendor_feedback_body(
        req.RID,
        driverBehaviour=1.0,
        punctuality=2.0,
        carCondition=3.5,
        cleanliness=4.5,
    )
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=body)
    assert response.status_code == 201

    client_check = _client_for(engine, Session, OTHER_CUSTOMER)
    listing = client_check.get(
        "/getallreviewsforvendor", params={"VENDORID": VENDOR_ID}
    ).json()
    row = listing[0]
    assert row["driverBehaviour"] == 1.0
    assert row["punctuality"] == 2.0
    assert row["carCondition"] == 3.5
    assert row["carCondition"] != row["punctuality"]
    assert row["cleanliness"] == 4.5


def test_insert_vendor_feedback_marks_review_done_y(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 201
    updated = _get_request(Session, req.RID)
    assert updated.reviewDone == "Y"
    assert updated.customerReviewDone == "N"


def test_insert_vendor_feedback_updates_vendor_aggregate(seeded_db):
    db, engine, Session = seeded_db
    req1 = _seed_request(db, from_location="Gangtok", to_location="Siliguri")
    req2 = _seed_request(db, from_location="Pelling", to_location="Darjeeling")
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        client.post(
            "/insertfeedback",
            json=_vendor_feedback_body(
                req1.RID,
                driverBehaviour=4.0,
                punctuality=4.0,
                carCondition=4.0,
                cleanliness=4.0,
            ),
        )
        client.post(
            "/insertfeedback",
            json=_vendor_feedback_body(
                req2.RID,
                driverBehaviour=2.0,
                punctuality=2.0,
                carCondition=2.0,
                cleanliness=2.0,
            ),
        )
    vendor = _get_user(Session, VENDOR_ID)
    assert vendor.totalNoOfReviews == 2
    assert vendor.rating == "3.00"


@pytest.mark.parametrize("value", [0.5, 1.5, 2.5, 3.5, 4.5, 5.0])
def test_insert_vendor_feedback_half_values_accepted(seeded_db, value):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    body = _vendor_feedback_body(
        req.RID,
        driverBehaviour=value,
        punctuality=value,
        carCondition=value,
        cleanliness=value,
    )
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=body)
    assert response.status_code == 201


@pytest.mark.parametrize("bad_value", [0, 0.0, 5.5, 3.3, -1, 3.2])
def test_insert_vendor_feedback_rejects_invalid_ratings_422(seeded_db, bad_value):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    body = _vendor_feedback_body(req.RID, driverBehaviour=bad_value)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_RATING"


def test_insert_vendor_feedback_empty_comments_allowed(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    body = _vendor_feedback_body(req.RID, comments="")
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=body)
    assert response.status_code == 201
    client_check = _client_for(engine, Session, OTHER_CUSTOMER)
    listing = client_check.get(
        "/getallreviewsforvendor", params={"VENDORID": VENDOR_ID}
    ).json()
    assert listing[0]["comments"] == ""


def test_insert_vendor_feedback_long_comments_422(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    body = _vendor_feedback_body(req.RID, comments="x" * 1001)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_REVIEW_TEXT"


def test_insert_vendor_feedback_wrong_customer_403(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, customer_app_id=CUSTOMER_ID)
    client = _client_for(engine, Session, OTHER_CUSTOMER)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 403


def test_insert_vendor_feedback_missing_rid_404(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(999999))
    assert response.status_code == 404
    assert response.json()["detail"] == "REQUEST_NOT_FOUND"


def test_insert_vendor_feedback_future_pickup_409(seeded_db):
    db, engine, Session = seeded_db
    pd, pt = _tomorrow_pickup()
    req = _seed_request(db, pickup_date=pd, pickup_time=pt)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 409
    assert response.json()["detail"] == "TRIP_NOT_ELIGIBLE"


@pytest.mark.parametrize(
    "status_value",
    ["BID - OPEN", "BID - CONFIRMED", "BOOKING - CANCELLED BY USER"],
)
def test_insert_vendor_feedback_wrong_status_409(seeded_db, status_value):
    db, engine, Session = seeded_db
    req = _seed_request(db, status=status_value)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 409
    assert response.json()["detail"] == "TRIP_NOT_ELIGIBLE"


def test_insert_vendor_feedback_duplicate_409(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        first = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
        assert first.status_code == 201
        second = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert second.status_code == 409
    assert second.json()["detail"] == "ALREADY_REVIEWED"
    assert _count_vendor_reviews(Session, rid=req.RID) == 1


def test_insert_vendor_feedback_snapshot_called_with_vendor_flag(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch(
        "app_v1.crud.review.request_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 201
    refresh.assert_called_once_with(VENDOR_ID, flag="Vendor")


def test_insert_vendor_feedback_snapshot_failure_still_201(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=False):
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 201
    assert response.json()["message"] == "INSERTED"


def test_insert_vendor_feedback_missing_vendor_target_404_no_partial_write(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, vendor_app_id=MISSING_VENDOR)
    client = _client_for(engine, Session, CUSTOMER_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True) as refresh:
        response = client.post("/insertfeedback", json=_vendor_feedback_body(req.RID))
    assert response.status_code == 404
    assert response.json()["detail"] == "TARGET_NOT_FOUND"
    refresh.assert_not_called()
    assert _count_vendor_reviews(Session, rid=req.RID) == 0
    unchanged = _get_request(Session, req.RID)
    assert unchanged.reviewDone == "N"


# ---------------------------------------------------------------------------
# 5. POST /insertcustomerfeedback — vendor rates customer
# ---------------------------------------------------------------------------


def test_insert_customer_feedback_success_201(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert response.status_code == 201
    assert response.json() == {"message": "INSERTED"}


def test_insert_customer_feedback_marks_customer_review_done_y(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert response.status_code == 201
    updated = _get_request(Session, req.RID)
    assert updated.customerReviewDone == "Y"
    assert updated.reviewDone == "N"


def test_insert_customer_feedback_wrong_vendor_403(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, vendor_app_id=VENDOR_ID)
    client = _client_for(engine, Session, OTHER_VENDOR)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert response.status_code == 403


def test_insert_customer_feedback_updates_customer_aggregate(seeded_db):
    db, engine, Session = seeded_db
    req1 = _seed_request(db, from_location="Gangtok", to_location="Siliguri")
    req2 = _seed_request(db, from_location="Pelling", to_location="Darjeeling")
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        client.post(
            "/insertcustomerfeedback",
            json=_customer_feedback_body(req1.RID, RATING=5.0),
        )
        client.post(
            "/insertcustomerfeedback",
            json=_customer_feedback_body(req2.RID, RATING=3.0),
        )
    customer = _get_user(Session, CUSTOMER_ID)
    assert customer.totalCustomerReviews == 2
    assert customer.customerRating == "4.00"


def test_insert_customer_feedback_snapshot_called_with_customer_flag(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch(
        "app_v1.crud.review.request_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert response.status_code == 201
    refresh.assert_called_once_with(CUSTOMER_ID, flag="Customer")


def test_insert_customer_feedback_duplicate_409(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        first = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
        assert first.status_code == 201
        second = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert second.status_code == 409
    assert second.json()["detail"] == "ALREADY_REVIEWED"
    assert _count_customer_reviews(Session, rid=req.RID) == 1


def test_insert_customer_feedback_missing_target_404_no_partial_write(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, customer_app_id=MISSING_CUSTOMER, vendor_app_id=VENDOR_ID)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True) as refresh:
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "TARGET_NOT_FOUND"
    refresh.assert_not_called()
    assert _count_customer_reviews(Session, rid=req.RID) == 0
    unchanged = _get_request(Session, req.RID)
    assert unchanged.customerReviewDone == "N"


@pytest.mark.parametrize("value", [0.5, 2.5, 5.0])
def test_insert_customer_feedback_half_values_accepted(seeded_db, value):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback",
            json=_customer_feedback_body(req.RID, RATING=value),
        )
    assert response.status_code == 201


@pytest.mark.parametrize("bad_value", [0, 5.5, 3.3, -2])
def test_insert_customer_feedback_invalid_rating_422(seeded_db, bad_value):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback",
            json=_customer_feedback_body(req.RID, RATING=bad_value),
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_RATING"


def test_insert_customer_feedback_future_pickup_409(seeded_db):
    db, engine, Session = seeded_db
    pd, pt = _tomorrow_pickup()
    req = _seed_request(db, pickup_date=pd, pickup_time=pt)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(req.RID)
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "TRIP_NOT_ELIGIBLE"


def test_insert_customer_feedback_missing_rid_404(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback", json=_customer_feedback_body(999999)
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "REQUEST_NOT_FOUND"


def test_insert_customer_feedback_long_comments_422(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    client = _client_for(engine, Session, VENDOR_ID)
    with patch("app_v1.crud.review.request_snapshot_refresh", return_value=True):
        response = client.post(
            "/insertcustomerfeedback",
            json=_customer_feedback_body(req.RID, COMMENTS="y" * 1001),
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_REVIEW_TEXT"


# ---------------------------------------------------------------------------
# 7. Public-safe: cross-cutting checks across both GET endpoints
# ---------------------------------------------------------------------------


def test_public_safe_vendor_get_blob_excludes_secrets(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db)
    _seed_vendor_review(db, rid=req.RID, vendor_app_id=VENDOR_ID, comments="Nice trip")
    client = _client_for(engine, Session, OTHER_CUSTOMER)
    response = client.get("/getallreviewsforvendor", params={"VENDORID": VENDOR_ID})
    blob = response.text
    for secret in (SECRET_FCM, SECRET_BANK, SECRET_PASSWORD, "bankAccountNo", "password"):
        assert secret not in blob


def test_public_safe_customer_get_blob_excludes_secrets(seeded_db):
    db, engine, Session = seeded_db
    req = _seed_request(db, customer_app_id=CUSTOMER_ID)
    _seed_customer_review(
        db, rid=req.RID, giver_app_id=VENDOR_ID, receiver_app_id=CUSTOMER_ID
    )
    client = _client_for(engine, Session, CUSTOMER_ID)
    response = client.get("/getallreviewsforcustomer")
    blob = response.text
    for secret in (SECRET_FCM, SECRET_BANK, SECRET_PASSWORD, "bankAccountNo", "password"):
        assert secret not in blob
