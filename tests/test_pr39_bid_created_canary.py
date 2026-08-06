"""
PR39 bid.created canary — transactional outbox on insert_vendor_bid.
"""

from __future__ import annotations

import json
import os
import sys
import types
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
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.schemas.bid_details import VendorBidInsert  # noqa: E402
from app_v1.crud import vendor_bid as vendor_bid_mod  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

# Reuse PR11 fixtures/helpers
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


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    from sqlalchemy import event
    from app_v1.models.request_table import Request

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


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Base.metadata.create_all(bind=engine, tables=[DomainOutboxEvent.__table__])
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


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "true")


@pytest.fixture
def events_off(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")


def _outbox_rows(session):
    return session.query(DomainOutboxEvent).all()


def test_successful_bid_and_outbox_commit_atomically(seeded_db, bg, events_on):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="test-auth-subject-vendor-a",
    )
    assert result.message == "INSERTED"
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).filter(BidDetail.rID == req.RID).count() == 1
    rows = _outbox_rows(session2)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == "bid.created"
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(req.RID)
    assert ev.status == "pending"
    assert ev.payload["requestId"] == req.RID
    assert int(ev.payload["bidId"]) > 0
    assert session2.query(Request).filter(Request.RID == req.RID).one().noOfBids == 1
    dumped = json.dumps(ev.payload)
    assert CUSTOMER_ID not in dumped
    assert VENDOR_A not in dumped
    assert "fcm" not in dumped.lower()
    assert "token" not in dumped.lower()
    assert ev.actorAuthSubjectHash
    assert "test-auth-subject" not in (ev.actorAuthSubjectHash or "")


def test_outbox_failure_rolls_back_bid(seeded_db, bg, events_on):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    with patch(
        "app_v1.crud.vendor_bid.maybe_append_domain_event",
        side_effect=RuntimeError("outbox insert failed"),
    ):
        result = vendor_bid_mod.insert_vendor_bid(
            session,
            bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
            actor_auth_subject="subj",
        )
    assert result.message == "ERROR"
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).filter(BidDetail.rID == req.RID).count() == 0
    assert session2.query(DomainOutboxEvent).count() == 0
    assert session2.query(Request).filter(Request.RID == req.RID).one().noOfBids == 0
    assert bg.tasks == []


def test_duplicate_bid_creates_no_event(seeded_db, bg, events_on):
    req = _seed_request(seeded_db, no_of_bids=1)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    _seed_bid(seeded_db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, car_id=car_id)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1500),
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="subj",
    )
    assert result.message == "BID ALREADY PRESENT"
    session2 = _reopen(seeded_db)
    assert session2.query(DomainOutboxEvent).count() == 0
    assert bg.tasks == []


def test_invalid_ownership_creates_no_event(seeded_db, bg, events_on):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_B)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            session,
            bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
            actor_auth_subject="subj",
        )
    assert exc.value.status_code == 403
    session2 = _reopen(seeded_db)
    assert session2.query(DomainOutboxEvent).count() == 0
    assert session2.query(BidDetail).count() == 0


def test_invalid_status_creates_no_event(seeded_db, bg, events_on):
    req = _seed_request(seeded_db, status="BID - CONFIRMED")
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            session,
            bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
            actor_auth_subject="subj",
        )
    assert exc.value.status_code == 409
    assert _reopen(seeded_db).query(DomainOutboxEvent).count() == 0


def test_feature_flag_off_no_event_bid_succeeds(seeded_db, bg, events_off):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="subj",
    )
    assert result.message == "INSERTED"
    session2 = _reopen(seeded_db)
    assert session2.query(BidDetail).count() == 1
    assert session2.query(DomainOutboxEvent).count() == 0
    # Poller remains the only propagation path when flags are off.
    assert any(
        t.func is notifications_mod.notify_customer_new_bid for t in bg.tasks
    )


def test_feature_flag_partial_no_event(seeded_db, bg, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=900),
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="subj",
    )
    assert result.message == "INSERTED"
    assert _reopen(seeded_db).query(DomainOutboxEvent).count() == 0


def test_fcm_remains_post_commit(seeded_db, bg, events_on):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="subj",
    )
    assert result.message == "INSERTED"
    assert any(
        t.func is notifications_mod.notify_customer_new_bid for t in bg.tasks
    )
    assert any(
        t.func is notifications_mod.notify_other_vendors_new_bid for t in bg.tasks
    )


def test_event_ids_unique_across_bids(seeded_db, bg, events_on):
    req = _seed_request(seeded_db)
    car1 = _seed_car(seeded_db, user_app_id=VENDOR_A, reg="A1")
    car2 = _seed_car(seeded_db, user_app_id=VENDOR_A, reg="A2")
    s1 = _reopen(seeded_db)
    vendor_bid_mod.insert_vendor_bid(
        s1,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car1, bidAmount=1000),
        user_id=VENDOR_A,
        background_tasks=BackgroundTasks(),
        actor_auth_subject="subj",
    )
    s2 = _reopen(seeded_db)
    vendor_bid_mod.insert_vendor_bid(
        s2,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car2, bidAmount=1100),
        user_id=VENDOR_A,
        background_tasks=BackgroundTasks(),
        actor_auth_subject="subj",
    )
    rows = _reopen(seeded_db).query(DomainOutboxEvent).all()
    assert len(rows) == 2
    assert rows[0].eventId != rows[1].eventId


def test_no_direct_build_snapshot_or_refresh(seeded_db, bg, events_on):
    req = _seed_request(seeded_db)
    car_id = _seed_car(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    src = Path(vendor_bid_mod.__file__).read_text()
    # Callable usage only (docstrings may mention the legacy path).
    assert "from ..utils.vendor_snapshot_refresh" not in src
    assert "request_snapshot_refresh(" not in src
    assert '"/build_snapshot"' not in src and "'/build_snapshot'" not in src
    result = vendor_bid_mod.insert_vendor_bid(
        session,
        bid_data=VendorBidInsert(RID=req.RID, CARID=car_id, bidAmount=1200),
        user_id=VENDOR_A,
        background_tasks=bg,
        actor_auth_subject="subj",
    )
    assert result.message == "INSERTED"
