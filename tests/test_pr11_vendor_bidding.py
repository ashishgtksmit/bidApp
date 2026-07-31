"""
PR11 vendor bidding / handshake FastAPI tests.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
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
from app_v1.models.tags_table import Tag  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.crud.bid import get_bids_for_request  # noqa: E402
from app_v1.crud import vendor_bid as vendor_bid_mod  # noqa: E402
from app_v1.schemas.bid_details import (
    VendorBidInsert,
    BidAmountUpdate,
    VendorRejectBody,
)  # noqa: E402
from app_v1.endpoints.bid import router as bid_router  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.endpoints.car import router as car_router  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
VENDOR_C = "8637554389"

PR11_CORE_TABLES = [
    User.__table__,
    Request.__table__,
    Tag.__table__,
    LocationDetail.__table__,
]


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
                    imageVehicleSide TEXT
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


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR11_CORE_TABLES)
    _create_extra_sqlite(engine)


_bid_id_seq = {"n": 0}
_car_id_seq = {"n": 100}


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    req_counter = {"n": 0}
    _bid_id_seq["n"] = 0
    _car_id_seq["n"] = 100

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)


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
        gender="Male",
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
    )
    db.add(user)
    db.commit()
    return user


def _seed_location(db, *, lid: int, location: str, region_id: int = 1):
    row = LocationDetail(LID=lid, location=location, regionId=region_id)
    db.add(row)
    db.commit()
    return row


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "BID - OPEN",
    no_of_bids: int = 0,
    final_amount: int = 0,
    request_won_by=None,
    request_type: int = 1,
    from_location: str = "Gangtok",
    to_location: str = "Siliguri",
) -> Request:
    row = Request(
        fromLocation=from_location,
        fromLandmark="MG Marg",
        toLocation=to_location,
        toLandmark="NJP",
        pickUpDate=date(2026, 8, 15),
        pickUpTime=time(10, 30),
        noOfAdults=2,
        noOfKids=1,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest="Original",
        bidEndTime=datetime(2026, 8, 14, 18, 0, 0),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=request_type,
        noOfBids=no_of_bids,
        finalAmount=final_amount,
        WIZZPNR="WIZZ123",
        paymentStatus=None,
        requestWonBy=request_won_by,
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
    amount: float,
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


def _seed_car(
    db,
    *,
    user_app_id: str,
    approved: bool = True,
    reg: str = "SK01A1111",
    model: str = "Swift",
) -> int:
    _car_id_seq["n"] += 1
    car_id = _car_id_seq["n"]
    db.execute(
        text(
            """
            INSERT INTO cardetails
                (CARID, userAppId, carRegNo, carColor, carModel, modelYear, ownerName,
                 registrationDoc, powerOfAttorneyDoc, registeredOn, adminApproved,
                 carOwnedBySameVendor, CTD, imageVehicleFront, imageVehicleSide)
            VALUES
                (:car, :uid, :reg, 'White', :model, '2020', 'Owner',
                 'doc', NULL, :ts, :approved, 1, 1, 'front.png', NULL)
            """
        ),
        {
            "car": car_id,
            "uid": user_app_id,
            "reg": reg,
            "model": model,
            "ts": "2026-01-01 12:00:00",
            "approved": 1 if approved else 0,
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


def _reopen(session_like):
    engine = session_like.get_bind()
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded_db(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False, vendorApproved=False)
    _add_user(db, user_app_id=OTHER_ID, uid=2, alsoVendor=False, vendorApproved=False)
    _add_user(db, user_app_id=VENDOR_A, uid=3, full_name="Vendor A")
    _add_user(db, user_app_id=VENDOR_B, uid=4, full_name="Vendor B")
    _seed_location(db, lid=1, location="Gangtok")
    _seed_location(db, lid=2, location="Siliguri")
    return db


@pytest.fixture
def bg():
    return BackgroundTasks()


# ---------------------------------------------------------------------------
# Vendor GET bids
# ---------------------------------------------------------------------------


def test_vendor_get_bids_eligible_open_request(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=1)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=1500)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.get_bids_for_request_for_vendor(
        session, rid=req.RID, user_id=VENDOR_A
    )
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].BIDAMOUNT == 1500
    assert not hasattr(result[0], "FCMTOKEN") or "FCMTOKEN" not in result[0].model_dump()


def test_vendor_cannot_enumerate_ineligible_rid(seeded_db):
    # Vendor C has no city prefs / wrong prefs
    _add_user(
        seeded_db,
        user_app_id=VENDOR_C,
        uid=5,
        cityPreferences="",
        requestTypePreferences="",
    )
    req = _seed_request(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.get_bids_for_request_for_vendor(
            session, rid=req.RID, user_id=VENDOR_C
        )
    assert exc.value.status_code == 403


def test_customer_get_ownership_unchanged(seeded_db):
    req = _seed_request(seeded_db)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        get_bids_for_request(session, rid=req.RID, user_id=VENDOR_A)
    assert exc.value.status_code == 403


def test_vendor_get_missing_rid_404(seeded_db):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.get_bids_for_request_for_vendor(
            session, rid=99999, user_id=VENDOR_A
        )
    assert exc.value.status_code == 404


def test_vendor_get_invalid_status_409(seeded_db):
    req = _seed_request(seeded_db, status="BID - CONFIRMED")
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.get_bids_for_request_for_vendor(
            session, rid=req.RID, user_id=VENDOR_A
        )
    assert exc.value.status_code == 409


def test_vendor_get_only_open_bids_sorted(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=3)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=2000, car_id=1)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=1000, car_id=2)
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_B,
        amount=500,
        status="BID - CONFIRMED",
        car_id=3,
    )
    session = _reopen(seeded_db)
    result = vendor_bid_mod.get_bids_for_request_for_vendor(
        session, rid=req.RID, user_id=VENDOR_A
    )
    assert [b.BIDAMOUNT for b in result] == [1000.0, 2000.0]


def test_vendor_get_empty_list(seeded_db):
    req = _seed_request(seeded_db)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.get_bids_for_request_for_vendor(
        session, rid=req.RID, user_id=VENDOR_A
    )
    assert result == []


def test_vendor_get_via_existing_bid_without_city_prefs(seeded_db):
    _add_user(
        seeded_db,
        user_app_id=VENDOR_C,
        uid=5,
        cityPreferences="",
        requestTypePreferences="",
    )
    req = _seed_request(seeded_db, no_of_bids=1)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_C, amount=900)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.get_bids_for_request_for_vendor(
        session, rid=req.RID, user_id=VENDOR_C
    )
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Vendor cars
# ---------------------------------------------------------------------------


def test_vendor_cars_jwt_own_approved(seeded_db):
    _seed_car(seeded_db, user_app_id=VENDOR_A, approved=True)
    _seed_car(seeded_db, user_app_id=VENDOR_A, approved=False, reg="SK01X")
    _seed_car(seeded_db, user_app_id=VENDOR_B, approved=True, reg="SK02")
    session = _reopen(seeded_db)
    result = vendor_bid_mod.get_vendor_cars_for_bidding(session, user_id=VENDOR_A)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].CARREGNO == "SK01A1111"


def test_vendor_cars_mismatched_userAppId_403(seeded_db):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.get_vendor_cars_for_bidding(
            session, user_id=VENDOR_A, user_app_id=VENDOR_B
        )
    assert exc.value.status_code == 403


def test_vendor_cars_empty(seeded_db):
    session = _reopen(seeded_db)
    result = vendor_bid_mod.get_vendor_cars_for_bidding(session, user_id=VENDOR_A)
    assert result == []


def test_non_vendor_cars_403(seeded_db):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.get_vendor_cars_for_bidding(session, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Insert bid
# ---------------------------------------------------------------------------


def test_insert_bid_success_and_noOfBids(seeded_db, bg):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "INSERTED"
    session2 = _reopen(seeded_db)
    bids = session2.query(BidDetail).filter(BidDetail.rID == req.RID).all()
    assert len(bids) == 1
    assert str(bids[0].bidderID) == VENDOR_A
    assert bids[0].bidStatus == "BID - OPEN"
    req2 = session2.query(Request).filter(Request.RID == req.RID).one()
    assert req2.noOfBids == 1
    assert any(
        t.func is notifications_mod.notify_customer_new_bid for t in bg.tasks
    )


def test_insert_duplicate_same_car_already_present(seeded_db, bg):
    req = _seed_request(seeded_db, no_of_bids=1)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, car_id=car_id)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1500),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "BID ALREADY PRESENT"
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).filter(BidDetail.rID == req.RID).count() == 1
    assert session2.query(Request).filter(Request.RID == req.RID).one().noOfBids == 1
    assert bg.tasks == []


def test_insert_different_car_allowed(seeded_db, bg):
    req = _seed_request(seeded_db, no_of_bids=1)
    car1 = _seed_car(seeded_db, user_app_id=VENDOR_A, reg="A1")
    car2 = _seed_car(seeded_db, user_app_id=VENDOR_A, reg="A2")
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, car_id=car1)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car2, bidAmount=1100),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "INSERTED"
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).filter(BidDetail.rID == req.RID).count() == 2
    assert session2.query(Request).filter(Request.RID == req.RID).one().noOfBids == 2


def test_insert_other_vendor_car_403(seeded_db, bg):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_B)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            session,
            bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


def test_insert_zero_amount_validation():
    with pytest.raises(Exception):
        VendorBidInsert(RID=1, CARID=1, bidAmount=0)


def test_insert_missing_request_404(seeded_db, bg):
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            session,
            bid_data=VendorBidInsert(RID=99999, CARID=car_id, bidAmount=100),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 404


def test_insert_invalid_request_status_409(seeded_db, bg):
    req = _seed_request(seeded_db, status="REQUEST - CONFIRMED")
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            session,
            bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=100),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Update / Delete
# ---------------------------------------------------------------------------


def test_update_own_bid(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=1)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.update_vendor_bid(
        session,
        bid_id=bid.BID,
        body=BidAmountUpdate(bidAmount=1400),
        user_id=VENDOR_A,
    )
    assert result.message == "UPDATED"
    session2 = _reopen(seeded_db)
    assert float(session2.query(BidDetail).get(bid.BID).bidAmount) == 1400


def test_update_wrong_owner_403(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=1)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.update_vendor_bid(
            session,
            bid_id=bid.BID,
            body=BidAmountUpdate(bidAmount=1400),
            user_id=VENDOR_B,
        )
    assert exc.value.status_code == 403


def test_delete_own_bid_recomputes(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=2)
    b1 = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, car_id=1)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=1100, car_id=2)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.delete_vendor_bid(session, bid_id=b1.BID, user_id=VENDOR_A)
    assert result.message == "DELETED"
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).filter(BidDetail.BID == b1.BID).first() is None
    assert session2.query(Request).filter(Request.RID == req.RID).one().noOfBids == 1


def test_delete_missing_404(seeded_db):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.delete_vendor_bid(session, bid_id=99999, user_id=VENDOR_A)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Accept / Reject handshake
# ---------------------------------------------------------------------------


def test_vendor_accept_success(seeded_db, bg):
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=2)
    bid = _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1750.4,
        status="BID - CONFIRMED",
        car_id=1,
    )
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_B,
        amount=2000,
        status="BID - OPEN",
        car_id=2,
    )
    session = _reopen(seeded_db)
    result = vendor_bid_mod.accept_request_by_vendor(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    session2 = _reopen(seeded_db)
    req2 = session2.query(Request).filter(Request.RID == req.RID).one()
    assert req2.requestStatus == "REQUEST - CONFIRMED"
    assert req2.requestWonBy == VENDOR_A
    assert req2.finalAmount == 1750
    bid2 = session2.query(BidDetail).filter(BidDetail.BID == bid.BID).one()
    assert bid2.bidStatus == "REQUEST - CONFIRMED"
    other = (
        session2.query(BidDetail)
        .filter(BidDetail.rID == req.RID, BidDetail.BID != bid.BID)
        .one()
    )
    assert other.bidStatus == "BID - OPEN"
    assert any(
        t.func is notifications_mod.notify_customer_vendor_accepted for t in bg.tasks
    )


def test_vendor_accept_idempotent_replay(seeded_db, bg):
    req = _seed_request(
        seeded_db,
        status="REQUEST - CONFIRMED",
        no_of_bids=1,
        request_won_by=VENDOR_A,
        final_amount=1000,
    )
    bid = _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="REQUEST - CONFIRMED",
    )
    session = _reopen(seeded_db)
    result = vendor_bid_mod.accept_request_by_vendor(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    assert bg.tasks == []


def test_vendor_accept_wrong_vendor_403(seeded_db, bg):
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=1)
    bid = _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="BID - CONFIRMED",
    )
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.accept_request_by_vendor(
            session,
            rid=req.RID,
            bid_id=bid.BID,
            user_id=VENDOR_B,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


def test_vendor_reject_success(seeded_db, bg):
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=2)
    bid = _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="BID - CONFIRMED",
        car_id=1,
    )
    other = _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_B,
        amount=1100,
        status="BID - OPEN",
        car_id=2,
    )
    session = _reopen(seeded_db)
    result = vendor_bid_mod.reject_request_by_vendor_pr11(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        body=VendorRejectBody(rejectionReason="Car unavailable"),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    session2 = _reopen(seeded_db)
    req2 = session2.query(Request).filter(Request.RID == req.RID).one()
    assert req2.requestStatus == "BID - OPEN"
    assert req2.rejectionReason == "Car unavailable"
    assert req2.requestWonBy is None
    assert session2.query(BidDetail).filter(BidDetail.BID == bid.BID).first() is None
    assert (
        session2.query(BidDetail).filter(BidDetail.BID == other.BID).one().bidStatus
        == "BID - OPEN"
    )
    assert req2.noOfBids == 1
    assert any(
        t.func is notifications_mod.notify_customer_vendor_rejected for t in bg.tasks
    )
    assert any(
        t.func is notifications_mod.notify_vendors_bidding_reopened for t in bg.tasks
    )


def test_vendor_reject_already_open_409(seeded_db, bg):
    req = _seed_request(seeded_db, status="BID - OPEN", no_of_bids=1)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.reject_request_by_vendor_pr11(
            session,
            rid=req.RID,
            bid_id=bid.BID,
            body=VendorRejectBody(rejectionReason="again"),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


def test_vendor_reject_empty_reason_validation():
    with pytest.raises(Exception):
        VendorRejectBody(rejectionReason="   ")


def test_notify_helpers_own_sessionlocal():
    src = Path(notifications_mod.__file__).read_text()
    for name in (
        "notify_customer_new_bid",
        "notify_other_vendors_new_bid",
        "notify_customer_vendor_accepted",
        "notify_losing_vendors_trip_won",
        "notify_customer_vendor_rejected",
        "notify_vendors_bidding_reopened",
    ):
        assert f"def {name}" in src
    assert "SessionLocal()" in src


# ---------------------------------------------------------------------------
# HTTP smoke
# ---------------------------------------------------------------------------


def test_http_routes_smoke(seeded_db):
    app = FastAPI()
    app.include_router(bid_router)
    app.include_router(request_router)
    app.include_router(car_router)

    def _override_db():
        try:
            yield seeded_db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: VENDOR_A

    client = TestClient(app)
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)

    r = client.get("/getallbidsforrequestforvendor", params={"RID": req.RID})
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/viewcarsforvendor")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = client.post(
        "/insertbid",
        json={"RID": req.RID, "CARID": car_id, "bidAmount": 1200},
    )
    assert r.status_code == 200
    assert r.json()["message"] == "INSERTED"

    bid = seeded_db.query(BidDetail).filter(BidDetail.rID == req.RID).one()
    r = client.put(
        "/updatebid",
        params={"BIDID": bid.BID},
        json={"bidAmount": 1300},
    )
    assert r.status_code == 200
    assert r.json()["message"] == "UPDATED"

    # Customer ownership route still present
    app.dependency_overrides[get_current_user_id] = lambda: CUSTOMER_ID
    r = client.get("/getallbidsforrequest", params={"RID": req.RID})
    assert r.status_code == 200
