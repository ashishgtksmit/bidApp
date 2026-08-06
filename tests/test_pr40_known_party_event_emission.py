"""
PR40 known-party domain-event emission — eight lifecycle mutations.

All per-event flags default false. Production enablement is not claimed here.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, text
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

from app_v1.database import Base  # noqa: E402
from app_v1.events.models import DomainOutboxEvent  # noqa: E402
from app_v1.events.outbox import (  # noqa: E402
    event_emission_enabled,
    event_type_enabled,
    env_flag_enabled,
)
from app_v1.events.registry import (  # noqa: E402
    EVENT_TYPE_FLAG_ENV,
    SUPPORTED_EVENT_TYPES,
)
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.models.driver_details import DriverDetail  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.schemas.bid_details import BidAmountUpdate, VendorRejectBody  # noqa: E402
from app_v1.schemas.request_table import AssignDriverRequest  # noqa: E402
from app_v1.crud import vendor_bid as vendor_bid_mod  # noqa: E402
from app_v1.crud import bid as bid_mod  # noqa: E402
from app_v1.crud import request as request_mod  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

from tests.test_pr11_vendor_bidding import (  # noqa: E402
    CUSTOMER_ID,
    VENDOR_A,
    VENDOR_B,
    _add_user,
    _prepare_engine,
    _reopen,
    _seed_car,
    _seed_location,
    _seed_request,
    _seed_bid,
    _bid_id_seq,
    _car_id_seq,
)

PR40_EVENT_FLAGS = [
    "DOMAIN_EVENT_BID_UPDATED_ENABLED",
    "DOMAIN_EVENT_BID_DELETED_ENABLED",
    "DOMAIN_EVENT_BID_ACCEPTED_ENABLED",
    "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED",
    "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED",
    "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED",
    "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED",
    "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED",
]


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    from sqlalchemy import event

    req_counter = {"n": 0}
    driver_counter = {"n": 0}
    _bid_id_seq["n"] = 0
    _car_id_seq["n"] = 100

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


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Base.metadata.create_all(
        bind=engine,
        tables=[DomainOutboxEvent.__table__, DriverDetail.__table__],
    )
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded_db(db):
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False, vendorApproved=False)
    _add_user(db, user_app_id=VENDOR_A, uid=3, full_name="Vendor A")
    _add_user(db, user_app_id=VENDOR_B, uid=4, full_name="Vendor B")
    _seed_location(db, lid=1, location="Gangtok")
    _seed_location(db, lid=2, location="Siliguri")
    return db


@pytest.fixture
def bg():
    return BackgroundTasks()


def _enable_event(monkeypatch, event_type: str):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    for name in PR40_EVENT_FLAGS:
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    flag = EVENT_TYPE_FLAG_ENV[event_type]
    monkeypatch.setenv(flag, "true")


def _disable_all(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    for name in PR40_EVENT_FLAGS:
        monkeypatch.setenv(name, "false")


def _outbox(session):
    return _reopen(session).query(DomainOutboxEvent).all()


def _assert_forbidden_absent(payload: dict, *extra: str):
    dumped = json.dumps(payload).lower()
    for bad in (
        "fcm",
        "token",
        "jwt",
        "password",
        "authsubject",
        "rejectionreason",
        "bidamount",
        "finalamount",
        *extra,
    ):
        assert bad not in dumped


def _seed_driver(db, *, user_app_id: str, number: str = "9800000001") -> DriverDetail:
    row = DriverDetail(
        userAppId=user_app_id,
        driverName="Driver",
        driverNumber=number,
        driverDOB=date(1990, 1, 1),
        driverGender="M",
        driverCity="Gangtok",
        driverLicense=f"LIC-{number}",
        driverDocument=f"DOC-{number}",
        driverPhoto="photo.jpg",
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _future_pickup_kwargs():
    pickup = datetime.now() + timedelta(days=3)
    return {"pickUpDate": pickup.date(), "pickUpTime": pickup.time().replace(microsecond=0)}


# --- Flag helpers ---


def test_pr40_flags_default_false(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    for name in PR40_EVENT_FLAGS:
        monkeypatch.delenv(name, raising=False)
    assert env_flag_enabled("DOMAIN_EVENTS_ENABLED") is False
    for event_type, flag in EVENT_TYPE_FLAG_ENV.items():
        if event_type == "bid.created":
            continue
        assert event_type_enabled(event_type) is False
        assert event_emission_enabled(event_type) is False
        assert flag.endswith("_ENABLED")


def test_master_off_blocks_even_when_event_flag_on(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv("DOMAIN_EVENT_BID_UPDATED_ENABLED", "true")
    assert event_emission_enabled("bid.updated") is False


def test_malformed_env_never_raises(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "maybe")
    monkeypatch.setenv("DOMAIN_EVENT_BID_UPDATED_ENABLED", "")
    assert env_flag_enabled("DOMAIN_EVENTS_ENABLED") is False
    assert event_emission_enabled("bid.updated") is False


# --- bid.updated ---


def test_bid_updated_emits_when_enabled(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.updated")
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.update_vendor_bid(
        session,
        bid_id=bid.BID,
        body=BidAmountUpdate(bidAmount=1500),
        user_id=VENDOR_A,
        actor_auth_subject="subj-a",
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == "bid.updated"
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(req.RID)
    assert ev.payload == {"requestId": req.RID, "bidId": bid.BID}
    _assert_forbidden_absent(ev.payload)
    assert VENDOR_A not in json.dumps(ev.payload)


def test_bid_updated_same_value_emits(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.updated")
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.update_vendor_bid(
        session,
        bid_id=bid.BID,
        body=BidAmountUpdate(bidAmount=1000),
        user_id=VENDOR_A,
    )
    assert result.message == "UPDATED"
    assert len(_outbox(seeded_db)) == 1


def test_bid_updated_flag_off_no_event(seeded_db, monkeypatch):
    _disable_all(monkeypatch)
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.update_vendor_bid(
        session,
        bid_id=bid.BID,
        body=BidAmountUpdate(bidAmount=1200),
        user_id=VENDOR_A,
    )
    assert result.message == "UPDATED"
    assert _outbox(seeded_db) == []
    session2 = _reopen(seeded_db)
    assert float(session2.query(BidDetail).one().bidAmount) == 1200.0


def test_bid_updated_master_on_event_off(seeded_db, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_UPDATED_ENABLED", "false")
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    vendor_bid_mod.update_vendor_bid(
        session,
        bid_id=bid.BID,
        body=BidAmountUpdate(bidAmount=1300),
        user_id=VENDOR_A,
    )
    assert _outbox(seeded_db) == []


def test_bid_updated_outbox_failure_rolls_back(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.updated")
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with patch(
        "app_v1.crud.vendor_bid.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        result = vendor_bid_mod.update_vendor_bid(
            session,
            bid_id=bid.BID,
            body=BidAmountUpdate(bidAmount=2000),
            user_id=VENDOR_A,
        )
    assert result.message == "ERROR"
    session2 = _reopen(seeded_db)
    assert float(session2.query(BidDetail).one().bidAmount) == 1000.0
    assert session2.query(DomainOutboxEvent).count() == 0


def test_bid_updated_auth_failure_no_event(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.updated")
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.update_vendor_bid(
            session,
            bid_id=bid.BID,
            body=BidAmountUpdate(bidAmount=1500),
            user_id=VENDOR_B,
        )
    assert exc.value.status_code == 403
    assert _outbox(seeded_db) == []


def test_bid_updated_invalid_status_no_event(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.updated")
    req = _seed_request(seeded_db, status="BID - CONFIRMED")
    bid = _seed_bid(
        seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED"
    )
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.update_vendor_bid(
            session,
            bid_id=bid.BID,
            body=BidAmountUpdate(bidAmount=1500),
            user_id=VENDOR_A,
        )
    assert exc.value.status_code == 409
    assert _outbox(seeded_db) == []


# --- bid.deleted ---


def test_bid_deleted_emits_with_bidder_id(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.deleted")
    req = _seed_request(seeded_db, no_of_bids=2)
    bid_a = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=1100, car_id=102)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.delete_vendor_bid(
        session, bid_id=bid_a.BID, user_id=VENDOR_A, actor_auth_subject="subj"
    )
    assert result.message == "DELETED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == "bid.deleted"
    assert ev.payload["requestId"] == req.RID
    assert ev.payload["bidId"] == bid_a.BID
    assert ev.payload["bidderId"] == VENDOR_A
    assert "rejectionReason" not in ev.payload
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).count() == 1
    assert session2.query(Request).one().noOfBids == 1


def test_bid_deleted_replay_404_no_event(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.deleted")
    req = _seed_request(seeded_db, no_of_bids=1)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    s1 = _reopen(seeded_db)
    vendor_bid_mod.delete_vendor_bid(s1, bid_id=bid.BID, user_id=VENDOR_A)
    s2 = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.delete_vendor_bid(s2, bid_id=bid.BID, user_id=VENDOR_A)
    assert exc.value.status_code == 404
    assert len(_outbox(seeded_db)) == 1


def test_bid_deleted_outbox_failure_rolls_back(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "bid.deleted")
    req = _seed_request(seeded_db, no_of_bids=1)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with patch(
        "app_v1.crud.vendor_bid.maybe_append_domain_event",
        side_effect=RuntimeError("fail"),
    ):
        result = vendor_bid_mod.delete_vendor_bid(
            session, bid_id=bid.BID, user_id=VENDOR_A
        )
    assert result.message == "ERROR"
    assert _reopen(seeded_db).query(BidDetail).count() == 1
    assert _reopen(seeded_db).query(DomainOutboxEvent).count() == 0


# --- bid.accepted ---


def test_bid_accepted_emits_on_transition(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "bid.accepted")
    req = _seed_request(seeded_db, no_of_bids=2)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=900, car_id=102)
    session = _reopen(seeded_db)
    result = bid_mod.accept_bid(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
        actor_auth_subject="cust",
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == "bid.accepted"
    assert rows[0].payload == {"requestId": req.RID, "bidId": bid.BID}
    assert any(t.func is notifications_mod.notify_vendor_bid_accepted for t in bg.tasks)


def test_bid_accepted_replay_no_event(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "bid.accepted")
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=1)
    bid = _seed_bid(
        seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED"
    )
    session = _reopen(seeded_db)
    result = bid_mod.accept_bid(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    assert _outbox(seeded_db) == []
    assert bg.tasks == []


def test_bid_accepted_outbox_failure_rolls_back(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "bid.accepted")
    req = _seed_request(seeded_db)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with patch(
        "app_v1.crud.bid.maybe_append_domain_event",
        side_effect=RuntimeError("fail"),
    ):
        result = bid_mod.accept_bid(
            session,
            rid=req.RID,
            bid_id=bid.BID,
            user_id=CUSTOMER_ID,
            background_tasks=bg,
        )
    assert result.message == "ERROR"
    assert _reopen(seeded_db).query(Request).one().requestStatus == "BID - OPEN"
    assert bg.tasks == []


# --- handshake.cancelled ---


def test_handshake_cancelled_emits_on_transition(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "handshake.cancelled")
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=2)
    _seed_bid(
        seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED"
    )
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=900, car_id=102)
    session = _reopen(seeded_db)
    result = request_mod.cancel_handshake(
        session, rid=req.RID, user_id=CUSTOMER_ID, actor_auth_subject="cust"
    )
    assert result.message == "CANCELLED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == "handshake.cancelled"
    assert rows[0].payload == {"requestId": req.RID}
    assert _reopen(seeded_db).query(Request).one().requestStatus == "BID - OPEN"


def test_handshake_cancelled_already_open_repair_no_event(seeded_db, monkeypatch):
    _enable_event(monkeypatch, "handshake.cancelled")
    req = _seed_request(seeded_db, status="BID - OPEN", no_of_bids=1)
    _seed_bid(
        seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED"
    )
    session = _reopen(seeded_db)
    result = request_mod.cancel_handshake(session, rid=req.RID, user_id=CUSTOMER_ID)
    assert result.message == "CANCELLED"
    assert _outbox(seeded_db) == []
    assert _reopen(seeded_db).query(BidDetail).one().bidStatus == "BID - OPEN"


# --- handshake.accepted ---


def test_handshake_accepted_emits_on_transition(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "handshake.accepted")
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=2)
    bid = _seed_bid(
        seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED"
    )
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=900, car_id=102)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.accept_request_by_vendor(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="vend",
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == "handshake.accepted"
    assert rows[0].payload == {"requestId": req.RID, "bidId": bid.BID}
    assert any(
        t.func is notifications_mod.notify_customer_vendor_accepted for t in bg.tasks
    )
    assert any(
        t.func is notifications_mod.notify_losing_vendors_trip_won for t in bg.tasks
    )


def test_handshake_accepted_replay_no_event(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "handshake.accepted")
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
    assert _outbox(seeded_db) == []
    assert bg.tasks == []


# --- handshake.rejected ---


def test_handshake_rejected_emits_once_with_bidder_id(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "handshake.rejected")
    req = _seed_request(seeded_db, status="BID - CONFIRMED", no_of_bids=2)
    bid = _seed_bid(
        seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED"
    )
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_B, amount=900, car_id=102)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.reject_request_by_vendor_pr11(
        session,
        rid=req.RID,
        bid_id=bid.BID,
        body=VendorRejectBody(rejectionReason="Cannot honour"),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == "handshake.rejected"
    assert ev.payload["bidderId"] == VENDOR_A
    assert "rejectionReason" not in ev.payload
    assert "Cannot honour" not in json.dumps(ev.payload)
    dumped = json.dumps([r.eventType for r in rows])
    assert "bid.deleted" not in dumped
    assert any(
        t.func is notifications_mod.notify_customer_vendor_rejected for t in bg.tasks
    )


def test_handshake_rejected_already_open_409_no_event(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "handshake.rejected")
    req = _seed_request(seeded_db, status="BID - OPEN", no_of_bids=1)
    bid = _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.reject_request_by_vendor_pr11(
            session,
            rid=req.RID,
            bid_id=bid.BID,
            body=VendorRejectBody(rejectionReason="x"),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409
    assert _outbox(seeded_db) == []
    assert bg.tasks == []


# --- booking.cancelled_by_customer ---


def test_booking_cancelled_emits_on_transition(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "booking.cancelled_by_customer")
    pickup = datetime.now() + timedelta(days=5)
    req = _seed_request(
        seeded_db,
        status="REQUEST - CONFIRMED",
        request_won_by=VENDOR_A,
        final_amount=1500,
        no_of_bids=1,
    )
    # Force future pickup via direct update (PR11 seed uses fixed past-ish dates).
    session = _reopen(seeded_db)
    session.query(Request).filter(Request.RID == req.RID).update(
        {
            Request.pickUpDate: pickup.date(),
            Request.pickUpTime: time(10, 0),
        },
        synchronize_session=False,
    )
    session.commit()
    session = _reopen(seeded_db)
    result = request_mod.booking_cancelled_by_user(
        session,
        rid=req.RID,
        rejection_reason="Plans changed",
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == "booking.cancelled_by_customer"
    assert rows[0].payload == {"requestId": req.RID}
    assert "Plans changed" not in json.dumps(rows[0].payload)
    assert any(
        t.func is notifications_mod.notify_vendor_booking_cancelled_by_customer
        for t in bg.tasks
    )


def test_booking_cancelled_replay_no_event(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "booking.cancelled_by_customer")
    req = _seed_request(
        seeded_db,
        status="BOOKING - CANCELLED BY USER",
        request_won_by=VENDOR_A,
    )
    session = _reopen(seeded_db)
    result = request_mod.booking_cancelled_by_user(
        session,
        rid=req.RID,
        rejection_reason="again",
        user_id=CUSTOMER_ID,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    assert _outbox(seeded_db) == []
    assert bg.tasks == []


# --- driver.assignment_changed ---


def test_driver_assignment_emits_on_change(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "driver.assignment_changed")
    req = _seed_request(
        seeded_db,
        status="REQUEST - CONFIRMED",
        request_won_by=VENDOR_A,
        final_amount=1000,
    )
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A, number="9800000101")
    session = _reopen(seeded_db)
    result = request_mod.assign_driver_to_request(
        session,
        AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == "driver.assignment_changed"
    assert rows[0].payload == {"requestId": req.RID, "driverId": driver.DDID}
    assert any(
        t.func is notifications_mod.notify_driver_assigned_to_customer_background
        for t in bg.tasks
    )


def test_driver_same_assignment_no_event(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "driver.assignment_changed")
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A, number="9800000102")
    req = _seed_request(
        seeded_db,
        status="REQUEST - CONFIRMED",
        request_won_by=VENDOR_A,
    )
    session = _reopen(seeded_db)
    session.query(Request).filter(Request.RID == req.RID).update(
        {Request.driverAssignedID: driver.DDID}, synchronize_session=False
    )
    session.commit()
    session = _reopen(seeded_db)
    result = request_mod.assign_driver_to_request(
        session,
        AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    assert _outbox(seeded_db) == []
    assert bg.tasks == []


def test_driver_replacement_emits_once(seeded_db, bg, monkeypatch):
    _enable_event(monkeypatch, "driver.assignment_changed")
    d1 = _seed_driver(seeded_db, user_app_id=VENDOR_A, number="9800000201")
    d2 = _seed_driver(seeded_db, user_app_id=VENDOR_A, number="9800000202")
    req = _seed_request(
        seeded_db,
        status="REQUEST - CONFIRMED",
        request_won_by=VENDOR_A,
    )
    session = _reopen(seeded_db)
    session.query(Request).filter(Request.RID == req.RID).update(
        {Request.driverAssignedID: d1.DDID}, synchronize_session=False
    )
    session.commit()
    session = _reopen(seeded_db)
    result = request_mod.assign_driver_to_request(
        session,
        AssignDriverRequest(RID=req.RID, DRIVERID=d2.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].payload["driverId"] == d2.DDID


def test_no_build_snapshot_in_pr40_mutation_sources():
    for mod in (vendor_bid_mod, bid_mod, request_mod):
        src = Path(mod.__file__).read_text()
        assert "request_snapshot_refresh(" not in src
        assert '"/build_snapshot"' not in src and "'/build_snapshot'" not in src


def test_supported_event_types_include_pr40():
    expected = {
        "bid.created",
        "bid.updated",
        "bid.deleted",
        "bid.accepted",
        "handshake.cancelled",
        "handshake.accepted",
        "handshake.rejected",
        "booking.cancelled_by_customer",
        "driver.assignment_changed",
    }
    assert SUPPORTED_EVENT_TYPES == expected
