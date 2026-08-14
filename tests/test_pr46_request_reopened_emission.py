"""
PR46 request.reopened FastAPI emission — transactional outbox on PUT /reopenbooking.

All flags default false. Production enablement is not claimed here.
Preserves existing PR12 reopen clone semantics + post-commit create-style FCM.
Asserts zero request.created duplication on successful reopen.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import types
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, event
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
    env_flag_enabled,
    event_emission_enabled,
    process_bound_flag_snapshot,
)
from app_v1.events.registry import (  # noqa: E402
    EVENT_REQUEST_CANCELLED,
    EVENT_REQUEST_CREATED,
    EVENT_REQUEST_REOPENED,
    EVENT_REQUEST_UPDATED,
    EVENT_TYPE_FLAG_ENV,
    SUPPORTED_EVENT_TYPES,
)
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.crud import request as request_mod  # noqa: E402
from app_v1.crud.request import reopen_request  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"
VENDOR_A = "8637554387"

PR46_TABLES = [User.__table__, Request.__table__, DomainOutboxEvent.__table__]

FLAG_REQUEST_REOPENED = "DOMAIN_EVENT_REQUEST_REOPENED_ENABLED"
STATUS_CANCELLED = "BOOKING - CANCELLED BY USER"
STATUS_OPEN = "BID - OPEN"


@pytest.fixture(autouse=True)
def _sqlite_assign_rid():
    counter = {"n": 0}

    def _assign(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            counter["n"] += 1
            target.RID = counter["n"]

    event.listen(Request, "before_insert", _assign)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign)


@pytest.fixture(autouse=True)
def _isolate_flags(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_REOPENED, raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_REQUEST_CREATED_ENABLED", raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_REQUEST_UPDATED_ENABLED", raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED", raising=False)
    yield


def _add_customer(db, *, user_app_id: str = CUSTOMER_ID, uid: int = 1):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="8637554387",
        fullName="Customer User",
        emailId=f"{user_app_id}@example.com",
        dob="1990-01-01",
        city="Gangtok",
        gender="Male",
        profilePicture="images/profilepic_male.png",
        alsoVendor=False,
        vendorApproved=False,
        lockApp=False,
        customerRating="4.5",
        totalCustomerReviews=12,
        rating="5",
        totalNoOfReviews=0,
        fcmToken=None,
        user_login_status="LOGGEDOUT",
    )
    db.add(user)
    db.commit()
    return user


def _seed_cancelled(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    request_reopened: bool = False,
    **overrides,
) -> Request:
    row = Request(
        fromLocation=overrides.get("fromLocation", "Gangtok"),
        fromLandmark=overrides.get("fromLandmark", "MG Marg"),
        toLocation=overrides.get("toLocation", "Siliguri"),
        toLandmark=overrides.get("toLandmark", "NJP"),
        pickUpDate=overrides.get("pickUpDate", date(2030, 8, 15)),
        pickUpTime=overrides.get("pickUpTime", time(10, 30)),
        noOfAdults=overrides.get("noOfAdults", 2),
        noOfKids=overrides.get("noOfKids", 1),
        carType=overrides.get("carType", "Sedan"),
        acRequest=overrides.get("acRequest", True),
        carrierRequest=overrides.get("carrierRequest", False),
        specialRequest=overrides.get("specialRequest", "Window seat"),
        bidEndTime=overrides.get(
            "bidEndTime", datetime(2030, 8, 14, 18, 0, 0)
        ),
        requestStatus=overrides.get("requestStatus", STATUS_CANCELLED),
        customerAppId=customer_app_id,
        requestType=overrides.get("requestType", 1),
        noOfBids=overrides.get("noOfBids", 1),
        finalAmount=overrides.get("finalAmount", 2500),
        WIZZPNR=overrides.get("WIZZPNR", "WIZZ123"),
        paymentStatus=overrides.get("paymentStatus", "PENDING"),
        requestWonBy=overrides.get("requestWonBy", VENDOR_A),
        rejectionReason=overrides.get("rejectionReason", "Change of Travel Plans"),
        requestReopened=request_reopened,
        driverAssignedID=overrides.get("driverAssignedID", 42),
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fresh(session_like):
    engine = session_like.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def _outbox(session_like):
    s = _fresh(session_like)
    try:
        return s.query(DomainOutboxEvent).all()
    finally:
        s.close()


def _enable_reopened(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_REOPENED, "true")


def _enable_created_too(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENT_REQUEST_CREATED_ENABLED", "true")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR46_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded_db(db):
    _add_customer(db)
    return db


@pytest.fixture()
def bg():
    return MagicMock(spec=BackgroundTasks)


def _reopen_ok(session_like, rid: int, bg, *, user_id: str = CUSTOMER_ID):
    with patch.object(request_mod, "get_vendors_for_request", return_value=[VENDOR_A]):
        session = _fresh(session_like)
        return reopen_request(
            session, r_id=rid, background_tasks=bg, user_id=user_id
        )


# --- Flag / emission matrix ---


def test_01_flag_absent_zero_event_business_ok(seeded_db, bg, monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_REOPENED, raising=False)
    src = _seed_cancelled(seeded_db)
    result = _reopen_ok(seeded_db, src.RID, bg)
    assert result.message == "UPDATED"
    assert result.newRequestId is not None
    assert _outbox(seeded_db) == []
    s = _fresh(seeded_db)
    try:
        original = s.query(Request).filter(Request.RID == src.RID).one()
        assert original.requestStatus == STATUS_CANCELLED
        assert bool(original.requestReopened) is True
        assert s.query(Request).filter(Request.RID == result.newRequestId).one().requestStatus == STATUS_OPEN
    finally:
        s.close()
    bg.add_task.assert_called_once()


def test_02_flag_false_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_REOPENED, "false")
    src = _seed_cancelled(seeded_db)
    assert _reopen_ok(seeded_db, src.RID, bg).message == "UPDATED"
    assert _outbox(seeded_db) == []


def test_03_master_false_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_REOPENED, "true")
    src = _seed_cancelled(seeded_db)
    assert _reopen_ok(seeded_db, src.RID, bg).message == "UPDATED"
    assert _outbox(seeded_db) == []
    assert event_emission_enabled(EVENT_REQUEST_REOPENED) is False


def test_04_05_06_07_both_true_exactly_one_reopened_envelope(
    seeded_db, bg, monkeypatch
):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db)
    result = _reopen_ok(seeded_db, src.RID, bg)
    assert result.message == "UPDATED"
    new_rid = int(result.newRequestId)
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == EVENT_REQUEST_REOPENED
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(new_rid)
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.payload == {"requestId": new_rid}
    assert ev.status == "pending"
    dumped = json.dumps(ev.payload).lower()
    for bad in (
        "sourcerequestid",
        "fcm",
        "token",
        "jwt",
        "password",
        "customerappid",
        "phone",
        "rejection",
        "reason",
        "fromlocation",
        "preference",
        "recipient",
        "vendor",
        "requestwonby",
        "finalamount",
    ):
        assert bad not in dumped
    assert CUSTOMER_ID not in json.dumps(ev.payload)
    assert str(src.RID) not in json.dumps(ev.payload)


def test_08_no_request_created_duplicate_even_if_created_flag_on(
    seeded_db, bg, monkeypatch
):
    _enable_reopened(monkeypatch)
    _enable_created_too(monkeypatch)
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    src = _seed_cancelled(seeded_db)
    result = _reopen_ok(seeded_db, src.RID, bg)
    assert result.message == "UPDATED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == EVENT_REQUEST_REOPENED
    assert all(r.eventType != EVENT_REQUEST_CREATED for r in rows)


def test_09_wrong_owner_403_zero_event(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db)
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, src.RID, bg, user_id=OTHER_ID)
    assert exc.value.status_code == 403
    s = _fresh(seeded_db)
    try:
        assert s.query(Request).count() == 1
        assert bool(s.query(Request).one().requestReopened) is False
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()
    bg.add_task.assert_not_called()


def test_10_missing_source_404_zero_event(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, 999999, bg)
    assert exc.value.status_code == 404
    assert _outbox(seeded_db) == []
    bg.add_task.assert_not_called()


def test_11_invalid_source_status_409_zero_event(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db, requestStatus=STATUS_OPEN)
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, src.RID, bg)
    assert exc.value.status_code == 409
    assert exc.value.detail == "INVALID_REQUEST_STATUS"
    assert _outbox(seeded_db) == []
    bg.add_task.assert_not_called()


def test_12_already_reopened_409_zero_event(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db, request_reopened=True)
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, src.RID, bg)
    assert exc.value.status_code == 409
    assert exc.value.detail == "REQUEST_ALREADY_REOPENED"
    assert _outbox(seeded_db) == []
    bg.add_task.assert_not_called()


def test_13_pickup_expired_409_zero_event(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(
        seeded_db,
        pickUpDate=date(2020, 1, 1),
        pickUpTime=time(10, 30),
    )
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, src.RID, bg)
    assert exc.value.status_code == 409
    assert exc.value.detail == "REOPEN_NOT_ALLOWED"
    assert _outbox(seeded_db) == []


def test_14_bid_end_expired_409_zero_event(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(
        seeded_db, bidEndTime=datetime(2020, 1, 1, 12, 0, 0)
    )
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, src.RID, bg)
    assert exc.value.status_code == 409
    assert exc.value.detail == "REOPEN_NOT_ALLOWED"
    assert _outbox(seeded_db) == []


def test_15_16_17_18_19_new_rid_and_field_parity(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db)
    original_rid = int(src.RID)
    won_by = src.requestWonBy
    final = src.finalAmount
    special = src.specialRequest
    pickup_date = src.pickUpDate
    pickup_time = src.pickUpTime
    bid_end = src.bidEndTime
    from_loc = src.fromLocation
    to_loc = src.toLocation

    result = _reopen_ok(seeded_db, original_rid, bg)
    assert result.message == "UPDATED"
    new_rid = int(result.newRequestId)
    assert new_rid != original_rid

    s = _fresh(seeded_db)
    try:
        original = s.query(Request).filter(Request.RID == original_rid).one()
        assert original.requestStatus == STATUS_CANCELLED
        assert bool(original.requestReopened) is True
        assert original.requestWonBy == won_by
        assert original.finalAmount == final

        new_req = s.query(Request).filter(Request.RID == new_rid).one()
        assert new_req.requestStatus == STATUS_OPEN
        assert bool(new_req.requestReopened) is False
        assert new_req.requestWonBy is None
        assert new_req.finalAmount in (0, None)
        assert new_req.noOfBids == 0
        assert new_req.rejectionReason is None
        assert new_req.driverAssignedID is None
        assert new_req.pickUpDate == pickup_date
        assert new_req.pickUpTime == pickup_time
        assert new_req.bidEndTime == bid_end
        assert new_req.specialRequest == special
        assert new_req.fromLocation == from_loc
        assert new_req.toLocation == to_loc
        assert new_req.customerAppId == CUSTOMER_ID
    finally:
        s.close()

    ev = _outbox(seeded_db)[0]
    assert ev.aggregateId == str(new_rid)
    assert ev.payload["requestId"] == new_rid


def test_20_21_22_outbox_failure_rolls_back_clone_flag_and_fcm(
    seeded_db, bg, monkeypatch
):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db)
    with patch.object(request_mod, "get_vendors_for_request", return_value=[VENDOR_A]):
        with patch(
            "app_v1.crud.request.maybe_append_domain_event",
            side_effect=RuntimeError("outbox failed"),
        ):
            session = _fresh(seeded_db)
            result = reopen_request(
                session, r_id=src.RID, background_tasks=bg, user_id=CUSTOMER_ID
            )
    assert result.message == "ERROR"
    s = _fresh(seeded_db)
    try:
        assert s.query(Request).count() == 1
        original = s.query(Request).one()
        assert original.RID == src.RID
        assert original.requestStatus == STATUS_CANCELLED
        assert bool(original.requestReopened) is False
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()
    bg.add_task.assert_not_called()


def test_23_fcm_only_after_commit(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db)
    result = _reopen_ok(seeded_db, src.RID, bg)
    assert result.message == "UPDATED"
    bg.add_task.assert_called_once()
    args, _kwargs = bg.add_task.call_args
    assert args[0] is notifications_mod.notify_vendors_for_request
    assert len(_outbox(seeded_db)) == 1


def test_24_replay_no_duplicate_rid_event_fcm(seeded_db, bg, monkeypatch):
    _enable_reopened(monkeypatch)
    src = _seed_cancelled(seeded_db)
    first = _reopen_ok(seeded_db, src.RID, bg)
    assert first.message == "UPDATED"
    assert len(_outbox(seeded_db)) == 1
    open_count = (
        _fresh(seeded_db)
        .query(Request)
        .filter(Request.requestStatus == STATUS_OPEN)
        .count()
    )
    assert open_count == 1

    bg2 = MagicMock(spec=BackgroundTasks)
    with pytest.raises(HTTPException) as exc:
        _reopen_ok(seeded_db, src.RID, bg2)
    assert exc.value.status_code == 409
    assert exc.value.detail == "REQUEST_ALREADY_REOPENED"
    assert len(_outbox(seeded_db)) == 1
    s = _fresh(seeded_db)
    try:
        assert s.query(Request).filter(Request.requestStatus == STATUS_OPEN).count() == 1
    finally:
        s.close()
    bg2.add_task.assert_not_called()


def test_25_process_bound_flag_snapshot(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_REOPENED, raising=False)
    assert EVENT_REQUEST_REOPENED == "request.reopened"
    assert EVENT_TYPE_FLAG_ENV[EVENT_REQUEST_REOPENED] == FLAG_REQUEST_REOPENED
    assert env_flag_enabled(FLAG_REQUEST_REOPENED) is False
    assert event_emission_enabled(EVENT_REQUEST_REOPENED) is False
    snap = process_bound_flag_snapshot(reason="unit")
    assert "requestReopened" in snap
    rr = snap["requestReopened"]
    assert rr["eventType"] == "request.reopened"
    assert rr["envFlag"] == FLAG_REQUEST_REOPENED
    assert rr["perEventEnabled"] is False
    assert rr["emissionEnabled"] is False

    _enable_reopened(monkeypatch)
    snap2 = process_bound_flag_snapshot(reason="unit")
    assert snap2["requestReopened"]["perEventEnabled"] is True
    assert snap2["requestReopened"]["emissionEnabled"] is True


def test_26_flag_off_business_behavior_unchanged(seeded_db, bg, monkeypatch):
    """With flags off, reopen still clones + sets requestReopened + schedules FCM."""
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_REOPENED, "false")
    src = _seed_cancelled(seeded_db)
    result = _reopen_ok(seeded_db, src.RID, bg)
    assert result.message == "UPDATED"
    assert result.newRequestId is not None
    assert _outbox(seeded_db) == []
    bg.add_task.assert_called_once()


def test_append_before_commit_ordering():
    lines = [
        ln.strip()
        for ln in inspect.getsource(request_mod.reopen_request).splitlines()
    ]
    append_at = next(
        i
        for i, ln in enumerate(lines)
        if ln.startswith("maybe_append_domain_event(")
    )
    commit_at = next(i for i, ln in enumerate(lines) if ln == "db.commit()")
    fcm_at = next(
        i
        for i, ln in enumerate(lines)
        if "notify_vendors_for_request" in ln and not ln.startswith("#")
    )
    assert append_at < commit_at < fcm_at


def test_registry_includes_reopened():
    assert EVENT_REQUEST_REOPENED in SUPPORTED_EVENT_TYPES
    assert EVENT_REQUEST_CREATED in SUPPORTED_EVENT_TYPES
    assert EVENT_REQUEST_UPDATED in SUPPORTED_EVENT_TYPES
    assert EVENT_REQUEST_CANCELLED in SUPPORTED_EVENT_TYPES
