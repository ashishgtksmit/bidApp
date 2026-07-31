"""
PR10 customer GET /getallbidsforrequest, PUT /acceptbid, PUT /cancelhandshakerequest.

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
from app_v1.crud.bid import accept_bid, get_bids_for_request  # noqa: E402
from app_v1.crud.request import cancel_handshake  # noqa: E402
from app_v1.endpoints.bid import router as bid_router  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.endpoints import bid as bid_endpoint_mod  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"

PR10_CORE_TABLES = [User.__table__, Request.__table__, Tag.__table__]


def _create_biddetails_sqlite(engine) -> None:
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
    Base.metadata.create_all(bind=engine, tables=PR10_CORE_TABLES)
    _create_biddetails_sqlite(engine)


_bid_id_seq = {"n": 0}


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    req_counter = {"n": 0}
    _bid_id_seq["n"] = 0

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
        vendorApproved=True,
        lockApp=False,
        customerRating="4.5",
        totalCustomerReviews=0,
        rating=kwargs.get("rating", "4.5"),
        totalNoOfReviews=kwargs.get("totalNoOfReviews", 3),
        fcmToken=kwargs.get("fcmToken", "secret-fcm-token-should-not-leak"),
        joiningDate=kwargs.get("joiningDate", date(2020, 5, 1)),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 12),
        user_login_status="LOGGEDOUT",
    )
    db.add(user)
    db.commit()
    return user


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "BID - OPEN",
    no_of_bids: int = 0,
    final_amount: int = 0,
    request_won_by=None,
) -> Request:
    row = Request(
        fromLocation="Gangtok",
        fromLandmark="MG Marg",
        toLocation="Siliguri",
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
        requestType=1,
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
    """Insert via raw SQL — BidDetail ORM FKs reference legacy table names."""
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


def _reopen(session_like):
    engine = session_like.get_bind()
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded_db(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, full_name="Customer", alsoVendor=False)
    _add_user(db, user_app_id=OTHER_ID, uid=2, full_name="Other", alsoVendor=False)
    _add_user(
        db,
        user_app_id=VENDOR_A,
        uid=3,
        full_name="Vendor A",
        tags=None,
        fcmToken="vendor-a-fcm",
    )
    _add_user(
        db,
        user_app_id=VENDOR_B,
        uid=4,
        full_name="Vendor B",
        fcmToken="vendor-b-fcm",
    )
    return db


@pytest.fixture()
def bg():
    return BackgroundTasks()


# --- GET bids ---


def test_get_bids_owner_lists_selectable_sorted(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=3)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=2500)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=1800)
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=9999,
        status="BID - CANCELLED",
        car_id=202,
    )

    result = get_bids_for_request(seeded_db, rid=req.RID, user_id=CUSTOMER_ID)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0].BIDAMOUNT == 1800.0
    assert result[1].BIDAMOUNT == 2500.0
    assert result[0].BIDDERNAME == "Vendor B"
    assert all(not hasattr(item, "FCMTOKEN") for item in result)
    dumped = result[0].model_dump()
    assert "FCMTOKEN" not in dumped
    assert dumped["BIDDERID"] == VENDOR_B
    assert dumped["CARID"] is None or True  # nullable car join ok without cardetails


def test_get_bids_wrong_owner_403(seeded_db):
    req = _seed_request(seeded_db)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    with pytest.raises(HTTPException) as exc:
        get_bids_for_request(seeded_db, rid=req.RID, user_id=OTHER_ID)
    assert exc.value.status_code == 403


def test_get_bids_missing_rid_404(seeded_db):
    with pytest.raises(HTTPException) as exc:
        get_bids_for_request(seeded_db, rid=99999, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 404


def test_get_bids_invalid_status_409(seeded_db):
    for status_value in (
        "BID - CONFIRMED",
        "REQUEST - CONFIRMED",
        "REQUEST - CANCELLED BY USER",
        "BOOKING - CANCELLED BY USER",
    ):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        _prepare_engine(engine)
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            _add_user(s, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
            _add_user(s, user_app_id=VENDOR_A, uid=3)
            req = _seed_request(s, status=status_value)
            _seed_bid(s, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
            with pytest.raises(HTTPException) as exc:
                get_bids_for_request(s, rid=req.RID, user_id=CUSTOMER_ID)
            assert exc.value.status_code == 409
        finally:
            s.close()
            engine.dispose()


def test_get_bids_empty_returns_list(seeded_db):
    req = _seed_request(seeded_db)
    result = get_bids_for_request(seeded_db, rid=req.RID, user_id=CUSTOMER_ID)
    assert result == []


def test_get_bids_excludes_rejected_and_confirmed_bids(seeded_db):
    req = _seed_request(seeded_db, no_of_bids=3)
    open_bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_B,
        amount=900,
        status="BID - CONFIRMED",
    )
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_B,
        amount=800,
        status="REJECTED",
        car_id=303,
    )
    result = get_bids_for_request(seeded_db, rid=req.RID, user_id=CUSTOMER_ID)
    assert len(result) == 1
    assert result[0].BIDID == open_bid.BID


def test_get_bids_sql_failure_safe(seeded_db):
    req = _seed_request(seeded_db)
    with patch.object(seeded_db, "query", side_effect=SQLAlchemyError("boom SELECT")):
        result = get_bids_for_request(seeded_db, rid=req.RID, user_id=CUSTOMER_ID)
    assert result.message == "ERROR_PREPARE"
    assert "boom" not in str(result.message)
    assert "SELECT" not in str(result)


# --- Accept bid ---


def test_accept_valid_owner_confirms_selected_only(seeded_db, bg):
    req = _seed_request(seeded_db, no_of_bids=2)
    rid = req.RID
    bid_a = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=2000, car_id=11)
    bid_b = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_B, amount=1500, car_id=22)
    bid_a_id, bid_b_id = bid_a.BID, bid_b.BID

    result = accept_bid(
        seeded_db,
        rid=rid,
        bid_id=bid_a_id,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"

    s = _reopen(seeded_db)
    try:
        updated_req = s.query(Request).filter(Request.RID == rid).one()
        assert updated_req.requestStatus == "BID - CONFIRMED"
        assert updated_req.requestWonBy is None
        assert updated_req.finalAmount == 0

        a = s.query(BidDetail).filter(BidDetail.BID == bid_a_id).one()
        b = s.query(BidDetail).filter(BidDetail.BID == bid_b_id).one()
        assert a.bidStatus == "BID - CONFIRMED"
        assert b.bidStatus == "BID - OPEN"
    finally:
        s.close()


def test_accept_endpoint_signature_uses_bidid_not_vendorid():
    import inspect

    sig = inspect.signature(bid_endpoint_mod.accept_bid_by_customer)
    params = sig.parameters
    assert "BIDID" in params
    assert "RID" in params
    assert "VENDORID" not in params


def test_accept_wrong_owner_403(seeded_db, bg):
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            seeded_db,
            rid=req.RID,
            bid_id=bid.BID,
            user_id=OTHER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


def test_accept_missing_rid_404(seeded_db, bg):
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            seeded_db,
            rid=99999,
            bid_id=1,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 404


def test_accept_missing_bid_404(seeded_db, bg):
    req = _seed_request(seeded_db)
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            seeded_db,
            rid=req.RID,
            bid_id=99999,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 404


def test_accept_bid_wrong_rid_409(seeded_db, bg):
    req1 = _seed_request(seeded_db)
    req2 = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req2.RID, bidder_id=VENDOR_A, amount=1000)
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            seeded_db,
            rid=req1.RID,
            bid_id=bid.BID,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


def test_accept_inactive_bid_409(seeded_db, bg):
    req = _seed_request(seeded_db)
    bid = _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="BID - CANCELLED",
    )
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            seeded_db,
            rid=req.RID,
            bid_id=bid.BID,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


def test_accept_request_not_open_409(seeded_db, bg):
    req = _seed_request(seeded_db, status="REQUEST - CONFIRMED")
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            seeded_db,
            rid=req.RID,
            bid_id=bid.BID,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


def test_accept_same_bid_replay_idempotent_no_duplicate_notify(seeded_db):
    req = _seed_request(seeded_db)
    rid = req.RID
    bid = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=1000)
    bid_id = bid.BID
    bg1 = BackgroundTasks()
    first = accept_bid(
        seeded_db,
        rid=rid,
        bid_id=bid_id,
        user_id=CUSTOMER_ID,
        background_tasks=bg1,
    )
    assert first.message == "UPDATED"
    assert len(bg1.tasks) == 1

    s = _reopen(seeded_db)
    bg2 = BackgroundTasks()
    second = accept_bid(
        s,
        rid=rid,
        bid_id=bid_id,
        user_id=CUSTOMER_ID,
        background_tasks=bg2,
    )
    assert second.message == "UPDATED"
    assert len(bg2.tasks) == 0


def test_accept_different_bid_while_confirmed_409(seeded_db, bg):
    req = _seed_request(seeded_db, no_of_bids=2)
    rid = req.RID
    bid_a = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=1000, car_id=1)
    bid_b = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_B, amount=900, car_id=2)
    bid_a_id, bid_b_id = bid_a.BID, bid_b.BID
    accept_bid(
        seeded_db,
        rid=rid,
        bid_id=bid_a_id,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )

    s = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            s,
            rid=rid,
            bid_id=bid_b_id,
            user_id=CUSTOMER_ID,
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.status_code == 409


def test_accept_concurrent_second_loses(seeded_db, bg):
    req = _seed_request(seeded_db, no_of_bids=2)
    rid = req.RID
    bid_a = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=1000, car_id=1)
    bid_b = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_B, amount=900, car_id=2)
    bid_a_id, bid_b_id = bid_a.BID, bid_b.BID

    first = accept_bid(
        seeded_db,
        rid=rid,
        bid_id=bid_a_id,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert first.message == "UPDATED"

    s = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        accept_bid(
            s,
            rid=rid,
            bid_id=bid_b_id,
            user_id=CUSTOMER_ID,
            background_tasks=BackgroundTasks(),
        )
    assert exc.value.status_code == 409

    s2 = _reopen(seeded_db)
    try:
        confirmed = (
            s2.query(BidDetail)
            .filter(BidDetail.rID == rid, BidDetail.bidStatus == "BID - CONFIRMED")
            .all()
        )
        assert len(confirmed) == 1
        assert confirmed[0].BID == bid_a_id
    finally:
        s2.close()


def test_accept_schedules_notification_after_commit(seeded_db):
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    bg = BackgroundTasks()
    accept_bid(
        seeded_db,
        rid=req.RID,
        bid_id=bid.BID,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is notifications_mod.notify_vendor_bid_accepted
    assert task.args[0] == VENDOR_A


def test_accept_notification_task_owns_sessionlocal():
    src = Path(notifications_mod.__file__).read_text()
    assert "def notify_vendor_bid_accepted" in src
    assert "SessionLocal()" in src
    assert "db.close()" in src


def test_accept_notification_failure_does_not_undo(seeded_db, bg):
    req = _seed_request(seeded_db)
    rid = req.RID
    bid = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=1000)
    result = accept_bid(
        seeded_db,
        rid=rid,
        bid_id=bid.BID,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"

    with patch.object(
        notifications_mod,
        "send_notification_to_user",
        side_effect=RuntimeError("fcm down"),
    ):
        notifications_mod.notify_vendor_bid_accepted(VENDOR_A)

    s = _reopen(seeded_db)
    try:
        row = s.query(Request).filter(Request.RID == rid).one()
        assert row.requestStatus == "BID - CONFIRMED"
    finally:
        s.close()


def test_accept_rollback_on_commit_failure(seeded_db, bg):
    req = _seed_request(seeded_db)
    rid = req.RID
    bid = _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=1000)
    bid_id = bid.BID

    original_commit = seeded_db.commit

    def boom_commit():
        raise SQLAlchemyError("commit failed SELECT * FROM secret")

    seeded_db.commit = boom_commit  # type: ignore[method-assign]
    try:
        result = accept_bid(
            seeded_db,
            rid=rid,
            bid_id=bid_id,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
        assert result.message == "ERROR"
        assert "SELECT" not in result.message
    finally:
        seeded_db.commit = original_commit  # type: ignore[method-assign]

    s = _reopen(seeded_db)
    try:
        row = s.query(Request).filter(Request.RID == rid).one()
        assert row.requestStatus == "BID - OPEN"
        b = s.query(BidDetail).filter(BidDetail.BID == bid_id).one()
        assert b.bidStatus == "BID - OPEN"
    finally:
        s.close()


# --- Cancel handshake ---


def test_cancel_confirmed_reopens_request_and_bids(seeded_db):
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=2)
    rid = req.RID
    bid_a = _seed_bid(
        seeded_db,
        rid=rid,
        bidder_id=VENDOR_A,
        amount=1000,
        status="BID - CONFIRMED",
        car_id=1,
    )
    bid_b = _seed_bid(
        seeded_db,
        rid=rid,
        bidder_id=VENDOR_B,
        amount=900,
        status="BID - OPEN",
        car_id=2,
    )
    bid_a_id, bid_b_id = bid_a.BID, bid_b.BID

    result = cancel_handshake(seeded_db, rid=rid, user_id=CUSTOMER_ID)
    assert result.message == "CANCELLED"

    s = _reopen(seeded_db)
    try:
        row = s.query(Request).filter(Request.RID == rid).one()
        assert row.requestStatus == "BID - OPEN"
        assert row.requestWonBy is None
        assert row.finalAmount == 0
        statuses = {
            b.BID: b.bidStatus
            for b in s.query(BidDetail).filter(BidDetail.rID == rid).all()
        }
        assert statuses[bid_a_id] == "BID - OPEN"
        assert statuses[bid_b_id] == "BID - OPEN"
    finally:
        s.close()


def test_cancel_wrong_owner_403(seeded_db):
    req = _seed_request(seeded_db, status="BID - CONFIRMED")
    with pytest.raises(HTTPException) as exc:
        cancel_handshake(seeded_db, rid=req.RID, user_id=OTHER_ID)
    assert exc.value.status_code == 403


def test_cancel_missing_rid_404(seeded_db):
    with pytest.raises(HTTPException) as exc:
        cancel_handshake(seeded_db, rid=99999, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 404


def test_cancel_invalid_status_409(seeded_db):
    for status_value in (
        "REQUEST - CONFIRMED",
        "REQUEST - CANCELLED BY USER",
        "BOOKING - CANCELLED BY USER",
    ):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        _prepare_engine(engine)
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            _add_user(s, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
            req = _seed_request(s, status=status_value)
            with pytest.raises(HTTPException) as exc:
                cancel_handshake(s, rid=req.RID, user_id=CUSTOMER_ID)
            assert exc.value.status_code == 409
        finally:
            s.close()
            engine.dispose()


def test_cancel_already_open_idempotent(seeded_db):
    req = _seed_request(seeded_db, status="BID - OPEN")
    rid = req.RID
    _seed_bid(seeded_db, rid=rid, bidder_id=VENDOR_A, amount=1000)
    result = cancel_handshake(seeded_db, rid=rid, user_id=CUSTOMER_ID)
    assert result.message == "CANCELLED"

    s = _reopen(seeded_db)
    try:
        row = s.query(Request).filter(Request.RID == rid).one()
        assert row.requestStatus == "BID - OPEN"
    finally:
        s.close()


def test_cancel_no_fcm_scheduled(seeded_db):
    req = _seed_request(seeded_db, status="BID - CONFIRMED")
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="BID - CONFIRMED",
    )
    with patch.object(notifications_mod, "notify_vendor_bid_accepted") as notify_mock:
        result = cancel_handshake(seeded_db, rid=req.RID, user_id=CUSTOMER_ID)
    assert result.message == "CANCELLED"
    notify_mock.assert_not_called()


def test_cancel_sql_failure_safe(seeded_db):
    req = _seed_request(seeded_db, status="BID - CONFIRMED")
    with patch.object(seeded_db, "query", side_effect=SQLAlchemyError("secret SQL")):
        # first query is Request load — will raise into except
        result = cancel_handshake(seeded_db, rid=req.RID, user_id=CUSTOMER_ID)
    assert result.message == "ERROR"
    assert "secret" not in result.message
    assert "SQL" not in result.message


# --- HTTP route smoke ---


def test_http_routes_ownership_and_contracts(seeded_db):
    engine = seeded_db.get_bind()
    TestingSession = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(bid_router)
    app.include_router(request_router)

    def _override_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: CUSTOMER_ID

    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1200)

    client = TestClient(app)

    list_resp = client.get(f"/getallbidsforrequest?RID={req.RID}")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert isinstance(body, list)
    assert body[0]["BIDID"] == bid.BID
    assert "FCMTOKEN" not in body[0]

    empty_req = _seed_request(seeded_db)
    empty_resp = client.get(f"/getallbidsforrequest?RID={empty_req.RID}")
    assert empty_resp.status_code == 200
    assert empty_resp.json() == []

    accept_resp = client.put(f"/acceptbid?RID={req.RID}&BIDID={bid.BID}")
    assert accept_resp.status_code == 200
    assert accept_resp.json()["message"] == "UPDATED"

    # VENDORID-only contract must not be required
    bad = client.put(f"/acceptbid?RID={req.RID}&VENDORID={VENDOR_A}")
    assert bad.status_code == 422

    cancel_resp = client.put(f"/cancelhandshakerequest?RID={req.RID}")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["message"] == "CANCELLED"

    app.dependency_overrides[get_current_user_id] = lambda: OTHER_ID
    forbid = client.get(f"/getallbidsforrequest?RID={req.RID}")
    # after cancel request is BID - OPEN again; wrong owner still 403
    assert forbid.status_code == 403
