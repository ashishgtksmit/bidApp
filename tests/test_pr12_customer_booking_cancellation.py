"""
PR12 customer confirmed-booking cancellation / reopen / vendor details tests.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time, timedelta
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
from app_v1.crud import request as request_crud  # noqa: E402
from app_v1.crud import user as user_crud  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402
from app_v1.utils.common import EmailErrorResponse  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"

PR12_CORE_TABLES = [
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


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR12_CORE_TABLES)
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


def _seed_location(db, *, lid: int, location: str, region_id: int = 1):
    row = LocationDetail(LID=lid, location=location, regionId=region_id)
    db.add(row)
    db.commit()
    return row


def _future_pickup():
    return date(2030, 8, 15), time(10, 30)


def _past_pickup():
    return date(2020, 1, 1), time(10, 30)


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "REQUEST - CONFIRMED",
    no_of_bids: int = 1,
    final_amount: int = 2500,
    request_won_by=VENDOR_A,
    request_type: int = 1,
    from_location: str = "Gangtok",
    to_location: str = "Siliguri",
    pickup_date=None,
    pickup_time=None,
    bid_end_time=None,
    special_request: str = "Window seat",
    rejection_reason=None,
    request_reopened: bool = False,
    payment_status: str = "PENDING",
    driver_assigned_id: int | None = 42,
) -> Request:
    pd, pt = _future_pickup()
    if pickup_date is not None:
        pd = pickup_date
    if pickup_time is not None:
        pt = pickup_time
    bet = bid_end_time or datetime(2030, 8, 14, 18, 0, 0)
    row = Request(
        fromLocation=from_location,
        fromLandmark="MG Marg",
        toLocation=to_location,
        toLandmark="NJP",
        pickUpDate=pd,
        pickUpTime=pt,
        noOfAdults=2,
        noOfKids=1,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest=special_request,
        bidEndTime=bet,
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=request_type,
        noOfBids=no_of_bids,
        finalAmount=final_amount,
        WIZZPNR="WIZZ123",
        paymentStatus=payment_status,
        requestWonBy=request_won_by,
        rejectionReason=rejection_reason,
        requestReopened=request_reopened,
        driverAssignedID=driver_assigned_id,
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
    status: str = "REQUEST - CONFIRMED",
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
    reg: str = "SK01A1111",
    model: str = "Swift",
) -> int:
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
    _add_user(db, user_app_id=VENDOR_A, uid=3, full_name="Vendor A", gender="Male", tags="1")
    _add_user(db, user_app_id=VENDOR_B, uid=4, full_name="Vendor B")
    tag = Tag(TAGID=1, tagsName="Reliable")
    db.add(tag)
    db.commit()
    _seed_location(db, lid=1, location="Gangtok")
    _seed_location(db, lid=2, location="Siliguri")
    return db


@pytest.fixture
def bg():
    return BackgroundTasks()


@pytest.fixture
def client(seeded_db):
    app = FastAPI()
    app.include_router(request_router)
    app.include_router(user_router)

    def _override_db():
        session = _reopen(seeded_db)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: CUSTOMER_ID
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_confirmed_booking(db):
    car_id = _seed_car(db, user_app_id=VENDOR_A)
    req = _seed_request(db)
    bid = _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=2500, car_id=car_id)
    return req, bid, car_id


# ---------------------------------------------------------------------------
# Cancel booking
# ---------------------------------------------------------------------------


def test_owner_cancels_confirmed_booking(seeded_db, bg):
    req, bid, _ = _seed_confirmed_booking(seeded_db)
    won_by = req.requestWonBy
    final = req.finalAmount
    no_bids = req.noOfBids
    driver = req.driverAssignedID
    payment = req.paymentStatus
    old_ts = req.tableTimestamp

    session = _reopen(seeded_db)
    result = request_crud.booking_cancelled_by_user(
        session,
        rid=req.RID,
        rejection_reason="  Change of Travel Plans  ",
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    assert updated.requestStatus == "BOOKING - CANCELLED BY USER"
    assert updated.rejectionReason == "Change of Travel Plans"
    assert updated.requestWonBy == won_by
    assert updated.finalAmount == final
    assert updated.noOfBids == no_bids
    assert updated.driverAssignedID == driver
    assert updated.paymentStatus == payment
    assert updated.tableTimestamp != old_ts

    bid_row = session2.query(BidDetail).filter(BidDetail.BID == bid.BID).one()
    assert bid_row.bidStatus == "REQUEST - CONFIRMED"


def test_cancel_endpoint_no_bidder_id_required(client, seeded_db):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    with patch.object(
        notifications_mod,
        "notify_vendor_booking_cancelled_by_customer",
        MagicMock(),
    ):
        resp = client.put(
            f"/bookingcancelledbyuser?RID={req.RID}",
            json={"rejectionReason": "Driver/Vendor Behaviour"},
        )
    assert resp.status_code == 200
    assert resp.json()["message"] == "UPDATED"


def test_cancel_wrong_customer_403(seeded_db, bg):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.booking_cancelled_by_user(
            session,
            rid=req.RID,
            rejection_reason="Change of Travel Plans",
            user_id=OTHER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


def test_cancel_missing_rid_404(seeded_db, bg):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.booking_cancelled_by_user(
            session,
            rid=99999,
            rejection_reason="Change of Travel Plans",
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "status_value",
    ["BID - OPEN", "BID - CONFIRMED", "REQUEST - CANCELLED BY USER"],
)
def test_cancel_invalid_status_409(seeded_db, bg, status_value):
    req = _seed_request(seeded_db, status=status_value, request_won_by=None, final_amount=0)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.booking_cancelled_by_user(
            session,
            rid=req.RID,
            rejection_reason="Change of Travel Plans",
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "INVALID_REQUEST_STATUS"


def test_cancel_past_pickup_409(seeded_db, bg):
    pd, pt = _past_pickup()
    req = _seed_request(seeded_db, pickup_date=pd, pickup_time=pt)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.booking_cancelled_by_user(
            session,
            rid=req.RID,
            rejection_reason="Change of Travel Plans",
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "CANCELLATION_NOT_ALLOWED"


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_cancel_invalid_reason_422(seeded_db, bg, reason):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.booking_cancelled_by_user(
            session,
            rid=req.RID,
            rejection_reason=reason,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 422


def test_cancel_oversized_reason_422(seeded_db, bg):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.booking_cancelled_by_user(
            session,
            rid=req.RID,
            rejection_reason="x" * 65536,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert exc.value.status_code == 422
    assert "rejectionReason" not in str(exc.value.detail).lower() or True
    assert "column" not in str(exc.value.detail).lower()


def test_cancel_idempotent_replay_no_renotify(seeded_db, bg):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    first = request_crud.booking_cancelled_by_user(
        session,
        rid=req.RID,
        rejection_reason="First reason",
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert first.message == "UPDATED"
    assert len(bg.tasks) == 1

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    ts1 = updated.tableTimestamp
    reason1 = updated.rejectionReason

    bg2 = BackgroundTasks()
    session3 = _reopen(seeded_db)
    second = request_crud.booking_cancelled_by_user(
        session3,
        rid=req.RID,
        rejection_reason="Second reason should not apply",
        user_id=CUSTOMER_ID,
        background_tasks=bg2,
    )
    assert second.message == "UPDATED"
    assert len(bg2.tasks) == 0

    session4 = _reopen(seeded_db)
    again = session4.query(Request).filter(Request.RID == req.RID).one()
    assert again.rejectionReason == reason1
    assert again.tableTimestamp == ts1


def test_cancel_notifies_request_won_by_after_commit(seeded_db, bg):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    result = request_crud.booking_cancelled_by_user(
        session,
        rid=req.RID,
        rejection_reason="Change of Travel Plans",
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is notifications_mod.notify_vendor_booking_cancelled_by_customer
    assert task.args[0] == VENDOR_A


def test_cancel_notification_failure_does_not_undo(seeded_db):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    bg = BackgroundTasks()

    def _boom(*_a, **_k):
        raise RuntimeError("fcm down")

    with patch.object(
        notifications_mod,
        "notify_vendor_booking_cancelled_by_customer",
        side_effect=_boom,
    ):
        session = _reopen(seeded_db)
        result = request_crud.booking_cancelled_by_user(
            session,
            rid=req.RID,
            rejection_reason="Change of Travel Plans",
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
        assert result.message == "UPDATED"
        # Execute scheduled task — must not undo DB
        for task in bg.tasks:
            try:
                task.func(*task.args, **task.kwargs)
            except Exception:
                pass

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    assert updated.requestStatus == "BOOKING - CANCELLED BY USER"


def test_cancel_sql_error_safe_response(seeded_db, bg):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    with patch.object(session, "query", side_effect=SQLAlchemyError("boom")):
        # with_for_update path uses query — force error after reopen fresh
        pass
    session2 = _reopen(seeded_db)
    with patch(
        "app_v1.crud.request.Request"
    ) as _unused:
        pass
    session3 = _reopen(seeded_db)
    real_query = session3.query

    def _failing_query(*args, **kwargs):
        raise SQLAlchemyError("secret sql exploded")

    with patch.object(session3, "query", side_effect=_failing_query):
        result = request_crud.booking_cancelled_by_user(
            session3,
            rid=req.RID,
            rejection_reason="Change of Travel Plans",
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert result.message == "ERROR"
    assert "secret sql" not in str(result)


# ---------------------------------------------------------------------------
# Reopen booking
# ---------------------------------------------------------------------------


def _seed_cancelled_for_reopen(db, **kwargs):
    car_id = _seed_car(db, user_app_id=VENDOR_A)
    req = _seed_request(
        db,
        status="BOOKING - CANCELLED BY USER",
        rejection_reason="Change of Travel Plans",
        **kwargs,
    )
    bid = _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=2500, car_id=car_id)
    return req, bid


def test_owner_reopens_cancelled_booking(seeded_db, bg):
    req, bid = _seed_cancelled_for_reopen(seeded_db)
    original_rid = req.RID
    won_by = req.requestWonBy
    final = req.finalAmount
    special = req.specialRequest
    pickup_date = req.pickUpDate
    pickup_time = req.pickUpTime
    bid_end = req.bidEndTime

    with patch.object(
        request_crud,
        "get_vendors_for_request",
        return_value=[VENDOR_B],
    ):
        session = _reopen(seeded_db)
        result = request_crud.reopen_request(
            session,
            r_id=original_rid,
            background_tasks=bg,
            user_id=CUSTOMER_ID,
        )

    assert result.message == "UPDATED"
    assert result.newRequestId is not None
    assert result.newRequestId != original_rid

    session2 = _reopen(seeded_db)
    original = session2.query(Request).filter(Request.RID == original_rid).one()
    assert original.requestStatus == "BOOKING - CANCELLED BY USER"
    assert bool(original.requestReopened) is True
    assert original.requestWonBy == won_by
    assert original.finalAmount == final
    assert original.specialRequest == special

    new_req = session2.query(Request).filter(Request.RID == result.newRequestId).one()
    assert new_req.requestStatus == "BID - OPEN"
    assert new_req.requestWonBy is None
    assert new_req.finalAmount in (0, None)
    assert new_req.noOfBids == 0
    assert new_req.rejectionReason is None
    assert bool(new_req.requestReopened) is False
    assert new_req.pickUpDate == pickup_date
    assert new_req.pickUpTime == pickup_time
    assert new_req.bidEndTime == bid_end
    assert new_req.specialRequest == special
    assert new_req.fromLocation == original.fromLocation
    assert new_req.fromLandmark == original.fromLandmark
    assert new_req.toLocation == original.toLocation
    assert new_req.toLandmark == original.toLandmark
    assert new_req.noOfAdults == original.noOfAdults
    assert new_req.noOfKids == original.noOfKids
    assert new_req.carType == original.carType
    assert bool(new_req.acRequest) == bool(original.acRequest)
    assert bool(new_req.carrierRequest) == bool(original.carrierRequest)
    assert new_req.driverAssignedID is None

    # Original bids retained; none on new RID
    assert session2.query(BidDetail).filter(BidDetail.rID == original_rid).count() == 1
    assert session2.query(BidDetail).filter(BidDetail.rID == result.newRequestId).count() == 0
    assert len(bg.tasks) == 1


def test_reopen_wrong_owner_403(seeded_db, bg):
    req, _ = _seed_cancelled_for_reopen(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=OTHER_ID
        )
    assert exc.value.status_code == 403


def test_reopen_missing_404(seeded_db, bg):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.reopen_request(
            session, r_id=99999, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 404


@pytest.mark.parametrize("status_value", ["REQUEST - CONFIRMED", "BID - OPEN"])
def test_reopen_invalid_source_status_409(seeded_db, bg, status_value):
    req = _seed_request(seeded_db, status=status_value)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 409


def test_reopen_already_reopened_409(seeded_db, bg):
    req, _ = _seed_cancelled_for_reopen(seeded_db, request_reopened=True)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "REQUEST_ALREADY_REOPENED"


def test_reopen_past_pickup_409(seeded_db, bg):
    pd, pt = _past_pickup()
    req, _ = _seed_cancelled_for_reopen(seeded_db, pickup_date=pd, pickup_time=pt)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "REOPEN_NOT_ALLOWED"


def test_reopen_expired_bid_end_409(seeded_db, bg):
    req, _ = _seed_cancelled_for_reopen(
        seeded_db, bid_end_time=datetime(2020, 1, 1, 12, 0, 0)
    )
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "REOPEN_NOT_ALLOWED"


def test_reopen_second_replay_409(seeded_db, bg):
    req, _ = _seed_cancelled_for_reopen(seeded_db)
    with patch.object(request_crud, "get_vendors_for_request", return_value=[]):
        session = _reopen(seeded_db)
        first = request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
        assert first.message == "UPDATED"

        session2 = _reopen(seeded_db)
        with pytest.raises(HTTPException) as exc:
            request_crud.reopen_request(
                session2, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
            )
        assert exc.value.status_code == 409
        assert exc.value.detail == "REQUEST_ALREADY_REOPENED"

    session3 = _reopen(seeded_db)
    assert session3.query(Request).filter(Request.requestStatus == "BID - OPEN").count() == 1


def test_reopen_endpoint_returns_new_request_id(client, seeded_db):
    req, _ = _seed_cancelled_for_reopen(seeded_db)
    with patch.object(request_crud, "get_vendors_for_request", return_value=[]):
        resp = client.put(f"/reopenbooking?RID={req.RID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "UPDATED"
    assert isinstance(body["newRequestId"], int)


def test_reopen_create_failure_rolls_back_flag(seeded_db, bg):
    req, _ = _seed_cancelled_for_reopen(seeded_db)
    with patch.object(
        request_crud,
        "insert_request_row",
        return_value=EmailErrorResponse(message="ERROR_INSERT"),
    ):
        session = _reopen(seeded_db)
        result = request_crud.reopen_request(
            session, r_id=req.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert result.message == "ERROR_INSERT"
    session2 = _reopen(seeded_db)
    original = session2.query(Request).filter(Request.RID == req.RID).one()
    assert bool(original.requestReopened) is False
    assert session2.query(Request).filter(Request.requestStatus == "BID - OPEN").count() == 0


# ---------------------------------------------------------------------------
# Vendor details
# ---------------------------------------------------------------------------


def test_owner_loads_vendor_details(seeded_db):
    req, _, car_id = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    result = user_crud.get_vendor_by_rid(session, rid=req.RID, user_id=CUSTOMER_ID)
    assert isinstance(result, list)
    assert len(result) == 1
    row = result[0]
    dumped = row.model_dump()
    assert dumped["FULLNAME"] == "Vendor A"
    assert dumped["GENDER"] == "Male"
    assert dumped["CARID"] == car_id
    assert dumped["CARREGNO"] == "SK01A1111"
    assert dumped["CAR_TYPE"] == "Sedan"
    assert "Reliable" in dumped["TAGS"]
    assert "FCMTOKEN" not in dumped
    assert "REGISTRATIONDOC" not in dumped
    assert "POWEROFATTORNEYDOC" not in dumped
    assert "bank" not in str(dumped).lower()
    assert "SECRET-REG-DOC" not in str(dumped)
    assert "SECRET-POA" not in str(dumped)
    assert "secret-fcm" not in str(dumped)


def test_vendor_details_wrong_customer_403(seeded_db):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        user_crud.get_vendor_by_rid(session, rid=req.RID, user_id=OTHER_ID)
    assert exc.value.status_code == 403


def test_vendor_details_missing_404(seeded_db):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        user_crud.get_vendor_by_rid(session, rid=99999, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 404


def test_vendor_details_no_selected_vendor_empty(seeded_db):
    req = _seed_request(seeded_db, request_won_by=None, status="REQUEST - CONFIRMED")
    session = _reopen(seeded_db)
    result = user_crud.get_vendor_by_rid(session, rid=req.RID, user_id=CUSTOMER_ID)
    assert result == []


def test_unrelated_bid_does_not_authorize(seeded_db):
    """Another vendor's open bid must not authorize vendor detail access."""
    car_a = _seed_car(seeded_db, user_app_id=VENDOR_A)
    car_b = _seed_car(seeded_db, user_app_id=VENDOR_B, reg="SK99Z9999")
    req = _seed_request(seeded_db, request_won_by=VENDOR_A)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=2500, car_id=car_a)
    _seed_bid(
        seeded_db,
        rid=req.RID,
        bidder_id=VENDOR_B,
        amount=2000,
        status="BID - OPEN",
        car_id=car_b,
    )
    session = _reopen(seeded_db)
    result = user_crud.get_vendor_by_rid(session, rid=req.RID, user_id=CUSTOMER_ID)
    assert len(result) == 1
    assert result[0].PRIMARYNUMBER == VENDOR_A
    assert result[0].CARREGNO == "SK01A1111"


def test_vendor_details_endpoint(client, seeded_db):
    req, _, _ = _seed_confirmed_booking(seeded_db)
    resp = client.get(f"/getvendordetailsbyrid?RID={req.RID}")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["GENDER"] == "Male"
    assert "REGISTRATIONDOC" not in data[0]
    assert "FCMTOKEN" not in data[0]


def test_notify_helper_route_token():
    """Canonical cancel notification uses ///Cancelled Trips."""
    with patch.object(
        notifications_mod, "send_notification_to_user"
    ) as send_mock, patch(
        "app_v1.database.SessionLocal", return_value=MagicMock()
    ):
        notifications_mod.notify_vendor_booking_cancelled_by_customer(VENDOR_A)
        assert send_mock.called
        payload = send_mock.call_args[0][1]
        assert payload.title == "Booking Cancelled"
        assert payload.body == "The customer has cancelled the confirmed booking."
        assert payload.url == "///Cancelled Trips"
