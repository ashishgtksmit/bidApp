"""
PR64 booking integrity — related-party and vehicle overlap guards.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")
os.environ["VENDOR_SUBSCRIPTION_ENABLED"] = "0"
os.environ["VENDOR_SUBSCRIPTION_ENFORCED"] = "0"

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.models.user_devices import UserDevice  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.models.tags_table import Tag  # noqa: E402
from app_v1.crud import vendor_bid as vendor_bid_mod  # noqa: E402
from app_v1.crud.bid import accept_bid  # noqa: E402
from app_v1.crud.review import create_vendor_review, insert_customer_review  # noqa: E402
from app_v1.crud.auth import login_user_auth  # noqa: E402
from app_v1.crud.user import fcm_token_update  # noqa: E402
from app_v1.schemas.bid_details import VendorBidInsert  # noqa: E402
from app_v1.schemas.user_table import UserLogin  # noqa: E402
from app_v1.schemas.vendor_reviews import ReviewCreate  # noqa: E402
from app_v1.schemas.customer_reviews import CreateCustomerReview  # noqa: E402
from app_v1.services.booking_integrity import (  # noqa: E402
    DETAIL_RELATED_PARTY,
    DETAIL_VEHICLE_ALREADY_BOOKED,
    assert_not_related_party,
    assert_vehicle_available,
    booking_window,
    upsert_user_device,
    windows_overlap,
)

CUSTOMER_ID = "7022359323"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
VENDOR_LINKED = "9111111111"

PR64_CORE_TABLES = [
    User.__table__,
    Request.__table__,
    Tag.__table__,
    LocationDetail.__table__,
    UserDevice.__table__,
]

_bid_id_seq = {"n": 0}
_car_id_seq = {"n": 100}


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
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS car_type_details (
                    CTD INTEGER PRIMARY KEY,
                    car_type VARCHAR(100) NOT NULL,
                    car_sub_type TEXT NOT NULL,
                    capacity VARCHAR(5) NOT NULL,
                    image_url TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS vendorreviews (
                    VRID INTEGER PRIMARY KEY,
                    customerAppId VARCHAR(10) NOT NULL,
                    RID INTEGER NOT NULL,
                    VENDORID INTEGER NOT NULL,
                    driverBehaviour NUMERIC(3,1),
                    punctuality NUMERIC(3,1),
                    carCondition NUMERIC(3,1),
                    cleanliness NUMERIC(3,1),
                    refreshments BOOLEAN,
                    comments TEXT,
                    tableTimestamp TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS customerreviews (
                    CR INTEGER PRIMARY KEY,
                    RID VARCHAR(20) NOT NULL,
                    ratingGiverUserAppId VARCHAR(20) NOT NULL,
                    ratingReceiverUserAppId VARCHAR(20) NOT NULL,
                    generalRating NUMERIC(3,1),
                    comments TEXT,
                    tableTimestamp TIMESTAMP
                )
                """
            )
        )


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine, tables=PR64_CORE_TABLES)
    _create_extra_sqlite(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    req_counter = {"n": 0}

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    _bid_id_seq["n"] = 0
    _car_id_seq["n"] = 100
    try:
        yield session
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        session.close()
        engine.dispose()


def _add_user(db, *, user_app_id: str, uid: int, **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber=kwargs.get("alternateNumber"),
        fullName=kwargs.get("full_name", "User"),
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
        totalNoOfReviews=3,
        fcmToken=kwargs.get("fcmToken", "fcm-a"),
        joiningDate=date(2020, 5, 1),
        user_login_status="LOGGEDOUT",
        cityPreferences="1",
        requestTypePreferences="1",
    )
    db.add(user)
    db.commit()
    return user


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "BID - OPEN",
    pick_up: date | None = None,
    trip_end: date | None = None,
    request_won_by=None,
    no_of_bids: int = 0,
) -> Request:
    row = Request(
        fromLocation="Gangtok",
        fromLandmark="MG Marg",
        toLocation="Siliguri",
        toLandmark="NJP",
        pickUpDate=pick_up or date(2026, 8, 15),
        pickUpTime=time(10, 30),
        noOfAdults=2,
        noOfKids=0,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest=None,
        bidEndTime=datetime(2026, 8, 14, 18, 0, 0),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=1,
        noOfBids=no_of_bids,
        finalAmount=0,
        requestWonBy=request_won_by,
        tripEndDate=trip_end,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_bid(
    db,
    *,
    rid: int,
    bidder_id: str,
    amount: float = 1000,
    status: str = "BID - OPEN",
    car_id: int | None = 101,
) -> BidDetail:
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
    return db.query(BidDetail).filter(BidDetail.BID == bid_id).one()


def _seed_car(db, *, user_app_id: str, reg: str = "SK01A1111") -> int:
    import re

    _car_id_seq["n"] += 1
    car_id = _car_id_seq["n"]
    normalized = re.sub(r"[^A-Z0-9]", "", str(reg).strip().upper())
    db.execute(
        text(
            """
            INSERT INTO cardetails
                (CARID, userAppId, carRegNo, normalizedCarRegNo, carColor, carModel, modelYear, ownerName,
                 registrationDoc, powerOfAttorneyDoc, registeredOn, adminApproved,
                 carOwnedBySameVendor, CTD, imageVehicleFront, imageVehicleSide,
                 isDeleted, deletedAt, deletedBy)
            VALUES
                (:car, :uid, :reg, :norm, 'White', 'Swift', '2020', 'Owner',
                 'doc', NULL, :ts, 1, 1, 1, 'front.png', NULL, 0, NULL, NULL)
            """
        ),
        {
            "car": car_id,
            "uid": user_app_id,
            "reg": reg,
            "norm": normalized,
            "ts": "2026-01-01 12:00:00",
        },
    )
    db.execute(
        text(
            """
            INSERT OR IGNORE INTO car_type_details (CTD, car_type, car_sub_type, capacity, image_url)
            VALUES (1, 'Sedan', 'Standard', '4', NULL)
            """
        )
    )
    db.commit()
    return car_id


def test_booking_window_and_overlap():
    req = Request(
        fromLocation="A",
        fromLandmark="a",
        toLocation="B",
        toLandmark="b",
        pickUpDate=date(2026, 8, 15),
        pickUpTime=time(10, 0),
        noOfAdults=1,
        noOfKids=0,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        requestStatus="BID - OPEN",
        customerAppId=CUSTOMER_ID,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
        tripEndDate=date(2026, 8, 17),
    )
    start, end = booking_window(req)
    assert start == date(2026, 8, 15)
    assert end == date(2026, 8, 17)
    assert windows_overlap(date(2026, 8, 16), date(2026, 8, 16), start, end)
    assert not windows_overlap(date(2026, 8, 18), date(2026, 8, 19), start, end)


def test_same_account_place_bid_related_party(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=True)
    req = _seed_request(db, customer_app_id=CUSTOMER_ID)
    car_id = _seed_car(db, user_app_id=CUSTOMER_ID)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            db,
            VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1500),
            user_id=CUSTOMER_ID,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == DETAIL_RELATED_PARTY


def test_alternate_number_phone_link_related_party(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False, alternateNumber=VENDOR_LINKED)
    _add_user(db, user_app_id=VENDOR_LINKED, uid=2, alsoVendor=True, alternateNumber="2000000000")
    with pytest.raises(HTTPException) as exc:
        assert_not_related_party(db, CUSTOMER_ID, VENDOR_LINKED)
    assert exc.value.detail == DETAIL_RELATED_PARTY


def test_shared_device_related_party(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    _add_user(db, user_app_id=VENDOR_A, uid=2, alsoVendor=True)
    upsert_user_device(db, CUSTOMER_ID, "device-shared-1")
    upsert_user_device(db, VENDOR_A, "device-shared-1")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        assert_not_related_party(db, CUSTOMER_ID, VENDOR_A)
    assert exc.value.detail == DETAIL_RELATED_PARTY


def test_accept_bid_blocks_related_party(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=True)
    req = _seed_request(db, customer_app_id=CUSTOMER_ID)
    car_id = _seed_car(db, user_app_id=CUSTOMER_ID)
    bid = _seed_bid(db, rid=req.RID, bidder_id=CUSTOMER_ID, car_id=car_id)
    with pytest.raises(HTTPException) as exc:
        accept_bid(db, rid=req.RID, bid_id=bid.BID, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 403
    assert exc.value.detail == DETAIL_RELATED_PARTY


def test_vehicle_overlap_at_handshake_and_confirm(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    _add_user(db, user_app_id=VENDOR_A, uid=2, alsoVendor=True)
    _add_user(db, user_app_id=VENDOR_B, uid=3, alsoVendor=False, alternateNumber="3000000000")

    car_id = _seed_car(db, user_app_id=VENDOR_A, reg="SK01BUSY01")

    # Existing confirmed booking on same car / same day.
    busy = _seed_request(
        db,
        customer_app_id=VENDOR_B,
        status="REQUEST - CONFIRMED",
        pick_up=date(2026, 8, 15),
        request_won_by=VENDOR_A,
    )
    _seed_bid(
        db,
        rid=busy.RID,
        bidder_id=VENDOR_A,
        status="REQUEST - CONFIRMED",
        car_id=car_id,
    )

    open_req = _seed_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="BID - OPEN",
        pick_up=date(2026, 8, 15),
    )
    open_bid = _seed_bid(
        db,
        rid=open_req.RID,
        bidder_id=VENDOR_A,
        status="BID - OPEN",
        car_id=car_id,
    )

    with pytest.raises(HTTPException) as exc:
        accept_bid(db, rid=open_req.RID, bid_id=open_bid.BID, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 409
    assert exc.value.detail == DETAIL_VEHICLE_ALREADY_BOOKED

    # Handshake-held car also blocks confirmation on another request.
    held = _seed_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="BID - CONFIRMED",
        pick_up=date(2026, 8, 20),
        trip_end=date(2026, 8, 22),
    )
    held_bid = _seed_bid(
        db,
        rid=held.RID,
        bidder_id=VENDOR_A,
        status="BID - CONFIRMED",
        car_id=car_id,
    )
    competing = _seed_request(
        db,
        customer_app_id=VENDOR_B,
        status="BID - CONFIRMED",
        pick_up=date(2026, 8, 21),
        trip_end=date(2026, 8, 21),
    )
    competing_bid = _seed_bid(
        db,
        rid=competing.RID,
        bidder_id=VENDOR_A,
        status="BID - CONFIRMED",
        car_id=car_id,
    )
    with pytest.raises(HTTPException) as exc2:
        vendor_bid_mod.accept_request_by_vendor(
            db,
            rid=competing.RID,
            bid_id=competing_bid.BID,
            user_id=VENDOR_A,
        )
    assert exc2.value.status_code == 409
    assert exc2.value.detail == DETAIL_VEHICLE_ALREADY_BOOKED

    # Non-overlapping date is allowed past the vehicle check helper.
    free = _seed_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="BID - OPEN",
        pick_up=date(2026, 9, 1),
    )
    assert_vehicle_available(db, car_id=car_id, request_row=free, exclude_rid=free.RID)


def test_cancelled_status_not_busy(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_user(db, user_app_id=VENDOR_A, uid=2)
    car_id = _seed_car(db, user_app_id=VENDOR_A)
    cancelled = _seed_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CANCELLED",
        pick_up=date(2026, 8, 15),
    )
    _seed_bid(
        db,
        rid=cancelled.RID,
        bidder_id=VENDOR_A,
        status="BID - OPEN",
        car_id=car_id,
    )
    candidate = _seed_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="BID - OPEN",
        pick_up=date(2026, 8, 15),
    )
    assert_vehicle_available(
        db, car_id=car_id, request_row=candidate, exclude_rid=candidate.RID
    )


def test_login_and_fcm_upsert_device(db):
    from app_v1.utils.security import hash_password

    user = _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    user.password = hash_password("secret")
    db.commit()
    engine = db.get_bind()

    # login_user_auth closes the session in finally — use a dedicated one.
    Session = sessionmaker(bind=engine)
    login_session = Session()
    try:
        result = login_user_auth(
            login_session,
            UserLogin(
                userAppId=CUSTOMER_ID, password="secret", deviceId="login-device-1"
            ),
            client_id=None,
        )
    except Exception:
        login_session.close()
        raise
    assert getattr(result, "message", None) in ("LOGIN SUCCESS", "ALREADY_LOGGEDIN")

    check = Session()
    try:
        rows = check.query(UserDevice).filter(UserDevice.userAppId == CUSTOMER_ID).all()
        assert any(r.deviceId == "login-device-1" for r in rows)
    finally:
        check.close()

    session2 = Session()
    try:
        fcm_token_update(
            session2,
            user_id=CUSTOMER_ID,
            fcm_token="new-fcm-token",
            device_id="fcm-device-2",
        )
        devices = (
            Session()
            .query(UserDevice)
            .filter(UserDevice.userAppId == CUSTOMER_ID)
            .all()
        )
        ids = {d.deviceId for d in devices}
        assert "fcm-device-2" in ids
    finally:
        session2.close()


def test_self_review_related_party(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=True)
    past = date.today() - timedelta(days=2)
    req = _seed_request(
        db,
        customer_app_id=CUSTOMER_ID,
        status="REQUEST - CONFIRMED",
        pick_up=past,
        request_won_by=CUSTOMER_ID,
    )
    with pytest.raises(HTTPException) as exc:
        create_vendor_review(
            db,
            ReviewCreate(
                RID=req.RID,
                driverBehaviour=5,
                punctuality=5,
                carCondition=5,
                cleanliness=5,
                comments="self",
            ),
            jwt_sub=CUSTOMER_ID,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == DETAIL_RELATED_PARTY

    with pytest.raises(HTTPException) as exc2:
        insert_customer_review(
            db,
            CreateCustomerReview(RID=req.RID, RATING=5, COMMENTS="self"),
            jwt_sub=CUSTOMER_ID,
        )
    assert exc2.value.status_code == 403
    assert exc2.value.detail == DETAIL_RELATED_PARTY
