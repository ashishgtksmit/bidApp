"""
PR45 request.cancelled FastAPI emission — transactional outbox on DELETE /deleterequest.

All flags default false. Production enablement is not claimed here.
Preserves existing PR9 soft-cancel + post-commit FCM to bidders.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
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
    EVENT_REQUEST_UPDATED,
    EVENT_TYPE_FLAG_ENV,
)
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.crud.request import delete_request  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"
BIDDER_ID = "8637554387"

PR45_CORE_TABLES = [User.__table__, Request.__table__, DomainOutboxEvent.__table__]

FLAG_REQUEST_CANCELLED = "DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED"


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
    monkeypatch.delenv(FLAG_REQUEST_CANCELLED, raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_REQUEST_CREATED_ENABLED", raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_REQUEST_UPDATED_ENABLED", raising=False)
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


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "BID - OPEN",
    no_of_bids: int = 0,
    **overrides,
) -> Request:
    row = Request(
        fromLocation=overrides.get("fromLocation", "Gangtok"),
        fromLandmark=overrides.get("fromLandmark", "MG Marg"),
        toLocation=overrides.get("toLocation", "Siliguri"),
        toLandmark=overrides.get("toLandmark", "NJP"),
        pickUpDate=overrides.get("pickUpDate", date(2026, 8, 15)),
        pickUpTime=overrides.get("pickUpTime", time(10, 30)),
        noOfAdults=overrides.get("noOfAdults", 2),
        noOfKids=overrides.get("noOfKids", 1),
        carType=overrides.get("carType", "Sedan"),
        acRequest=overrides.get("acRequest", True),
        carrierRequest=overrides.get("carrierRequest", False),
        specialRequest=overrides.get("specialRequest", "Original"),
        bidEndTime=overrides.get(
            "bidEndTime", datetime(2026, 8, 14, 18, 0, 0)
        ),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=overrides.get("requestType", 1),
        noOfBids=no_of_bids,
        finalAmount=0,
        WIZZPNR="WIZZ123",
        paymentStatus=None,
        requestWonBy=None,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
        rejectionReason=overrides.get("rejectionReason", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _reopen(session_like):
    engine = session_like.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def _outbox(session_like):
    s = _reopen(session_like)
    try:
        return s.query(DomainOutboxEvent).all()
    finally:
        s.close()


def _enable_request_cancelled(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_CANCELLED, "true")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR45_CORE_TABLES)
    _create_biddetails_sqlite(engine)
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


def _delete_ok(session, rid: int, bg, *, user_id: str = CUSTOMER_ID):
    return delete_request(
        session, r_id=rid, background_tasks=bg, user_id=user_id
    )


# --- Flag / emission matrix ---


def test_01_flag_absent_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_CANCELLED, raising=False)
    row = _seed_request(seeded_db)
    result = _delete_ok(seeded_db, row.RID, bg)
    assert result.message == "DELETED"
    assert _outbox(seeded_db) == []
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "REQUEST - CANCELLED BY USER"
    finally:
        s.close()
    bg.add_task.assert_called_once()


def test_02_flag_false_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_CANCELLED, "false")
    row = _seed_request(seeded_db)
    assert _delete_ok(seeded_db, row.RID, bg).message == "DELETED"
    assert _outbox(seeded_db) == []


def test_03_master_false_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_CANCELLED, "true")
    row = _seed_request(seeded_db)
    assert _delete_ok(seeded_db, row.RID, bg).message == "DELETED"
    assert _outbox(seeded_db) == []
    assert event_emission_enabled(EVENT_REQUEST_CANCELLED) is False


def test_04_both_enabled_exactly_one_event(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    assert _delete_ok(seeded_db, rid, bg).message == "DELETED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == EVENT_REQUEST_CANCELLED
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(rid)
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.payload == {"requestId": rid}
    assert ev.status == "pending"


def test_05_06_07_payload_schema_aggregate_exact(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    assert _delete_ok(seeded_db, rid, bg).message == "DELETED"
    ev = _outbox(seeded_db)[0]
    assert ev.payload == {"requestId": rid}
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(rid)
    dumped = json.dumps(ev.payload).lower()
    for bad in (
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
    ):
        assert bad not in dumped
    assert CUSTOMER_ID not in json.dumps(ev.payload)


def test_08_unauthorized_customer_403_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    with pytest.raises(HTTPException) as exc:
        _delete_ok(seeded_db, row.RID, bg, user_id=OTHER_ID)
    assert exc.value.status_code == 403
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "BID - OPEN"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()
    bg.add_task.assert_not_called()


def test_09_missing_rid_404_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _delete_ok(seeded_db, 999999, bg)
    assert exc.value.status_code == 404
    assert _outbox(seeded_db) == []
    bg.add_task.assert_not_called()


def test_10_invalid_lifecycle_409_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db, status="BID - CONFIRMED")
    with pytest.raises(HTTPException) as exc:
        _delete_ok(seeded_db, row.RID, bg)
    assert exc.value.status_code == 409
    assert _outbox(seeded_db) == []
    bg.add_task.assert_not_called()


def test_11_bids_present_still_cancels_and_emits(seeded_db, bg, monkeypatch):
    """
    Unlike /updaterequest, cancel does NOT gate on noOfBids==0.
    Existing bidders are allowed; bid rows retained; event still emits.
    """
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db, no_of_bids=1)
    rid = int(row.RID)
    seeded_db.execute(
        text(
            """
            INSERT INTO biddetails (BID, rID, bidderID, bidAmount, bidStatus)
            VALUES (1, :rid, :bidder, 1500.00, 'BID - OPEN')
            """
        ),
        {"rid": rid, "bidder": int(BIDDER_ID)},
    )
    seeded_db.commit()

    assert _delete_ok(seeded_db, rid, bg).message == "DELETED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == EVENT_REQUEST_CANCELLED
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "REQUEST - CANCELLED BY USER"
        assert s.query(BidDetail).count() == 1
        assert s.query(Request).one().noOfBids == 1
        assert s.query(Request).one().rejectionReason is None
    finally:
        s.close()


def test_12_replay_second_cancel_409_no_duplicate_lifecycle_event(
    seeded_db, bg, monkeypatch
):
    """Already-cancelled RID cannot cancel again → 409; no second event."""
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    assert _delete_ok(seeded_db, rid, bg).message == "DELETED"
    assert len(_outbox(seeded_db)) == 1

    bg2 = MagicMock(spec=BackgroundTasks)
    s = _reopen(seeded_db)
    try:
        with pytest.raises(HTTPException) as exc:
            delete_request(s, r_id=rid, background_tasks=bg2, user_id=CUSTOMER_ID)
        assert exc.value.status_code == 409
        assert s.query(DomainOutboxEvent).count() == 1
    finally:
        s.close()
    bg2.add_task.assert_not_called()


def test_13_outbox_failure_rolls_back_business_cancel(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    with patch(
        "app_v1.crud.request.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        result = delete_request(
            seeded_db, r_id=row.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert result.message == "DELETED ERROR IN FUNCTION"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "BID - OPEN"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_14_no_fcm_on_rollback(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    with patch(
        "app_v1.crud.request.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        result = delete_request(
            seeded_db, r_id=row.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert result.message == "DELETED ERROR IN FUNCTION"
    bg.add_task.assert_not_called()


def test_15_existing_fcm_after_commit_only(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    assert _delete_ok(seeded_db, rid, bg).message == "DELETED"
    bg.add_task.assert_called_once()
    args, _kwargs = bg.add_task.call_args
    from app_v1.services.notifications import notify_vendors_request_cancelled

    assert args[0] is notify_vendors_request_cancelled
    assert args[1] == rid
    assert len(_outbox(seeded_db)) == 1


def test_16_process_bound_flag_mapping(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_CANCELLED, raising=False)
    assert EVENT_REQUEST_CANCELLED == "request.cancelled"
    assert EVENT_TYPE_FLAG_ENV[EVENT_REQUEST_CANCELLED] == FLAG_REQUEST_CANCELLED
    assert env_flag_enabled(FLAG_REQUEST_CANCELLED) is False
    assert event_emission_enabled(EVENT_REQUEST_CANCELLED) is False
    snap = process_bound_flag_snapshot(reason="unit")
    assert "requestCancelled" in snap
    rx = snap["requestCancelled"]
    assert rx["eventType"] == "request.cancelled"
    assert rx["envFlag"] == FLAG_REQUEST_CANCELLED
    assert rx["perEventEnabled"] is False
    assert rx["emissionEnabled"] is False

    _enable_request_cancelled(monkeypatch)
    snap2 = process_bound_flag_snapshot(reason="unit")
    assert snap2["requestCancelled"]["perEventEnabled"] is True
    assert snap2["requestCancelled"]["emissionEnabled"] is True
    assert snap2["perEvent"][EVENT_REQUEST_CANCELLED] is True


def test_17_failed_commit_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    with patch.object(seeded_db, "commit", side_effect=SQLAlchemyError("boom")):
        result = delete_request(
            seeded_db, r_id=row.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert result.message == "DELETED ERROR IN FUNCTION"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "BID - OPEN"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()
    bg.add_task.assert_not_called()


def test_18_flag_off_business_behavior_unchanged(seeded_db, bg, monkeypatch):
    """
    Flag-off soft-cancel preserves PR9 semantics: status rewrite only in CRUD;
    rejectionReason untouched; FCM still scheduled post-commit; zero outbox.
    (tableTimestamp may still move via Column onupdate — not an explicit CRUD write.)
    """
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_CANCELLED, "false")
    row = _seed_request(seeded_db, no_of_bids=0)
    assert _delete_ok(seeded_db, row.RID, bg).message == "DELETED"
    s = _reopen(seeded_db)
    try:
        after = s.query(Request).one()
        assert after.requestStatus == "REQUEST - CANCELLED BY USER"
        assert after.rejectionReason is None
    finally:
        s.close()
    assert _outbox(seeded_db) == []
    bg.add_task.assert_called_once()


def test_19_reason_field_absent_from_event(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db, rejectionReason="should-not-appear")
    assert _delete_ok(seeded_db, row.RID, bg).message == "DELETED"
    ev = _outbox(seeded_db)[0]
    assert "reason" not in json.dumps(ev.payload).lower()
    assert "rejection" not in json.dumps(ev.payload).lower()
    assert set(ev.payload.keys()) == {"requestId"}


def test_20_no_duplicate_lifecycle_events_single_cancel(seeded_db, bg, monkeypatch):
    _enable_request_cancelled(monkeypatch)
    row = _seed_request(seeded_db)
    assert _delete_ok(seeded_db, row.RID, bg).message == "DELETED"
    types = [e.eventType for e in _outbox(seeded_db)]
    assert types == [EVENT_REQUEST_CANCELLED]


def test_created_or_updated_flag_alone_does_not_emit_cancelled(
    seeded_db, bg, monkeypatch
):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_REQUEST_CREATED_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_REQUEST_UPDATED_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_CANCELLED, "false")
    row = _seed_request(seeded_db)
    assert _delete_ok(seeded_db, row.RID, bg).message == "DELETED"
    assert _outbox(seeded_db) == []


def test_append_before_commit_ordering():
    """maybe_append_domain_event must appear before db.commit in delete_request."""
    import inspect
    from app_v1.crud import request as request_mod

    lines = [
        ln.strip()
        for ln in inspect.getsource(request_mod.delete_request).splitlines()
    ]
    append_at = next(
        i for i, ln in enumerate(lines) if ln.startswith("maybe_append_domain_event(")
    )
    commit_at = next(i for i, ln in enumerate(lines) if ln == "db.commit()")
    fcm_at = next(
        i for i, ln in enumerate(lines) if "notify_vendors_request_cancelled" in ln
        and not ln.startswith("#")
    )
    assert append_at < commit_at < fcm_at


def test_registry_includes_cancelled_and_siblings():
    from app_v1.events.registry import SUPPORTED_EVENT_TYPES

    assert EVENT_REQUEST_CANCELLED in SUPPORTED_EVENT_TYPES
    assert EVENT_REQUEST_CREATED in SUPPORTED_EVENT_TYPES
    assert EVENT_REQUEST_UPDATED in SUPPORTED_EVENT_TYPES
    # PR46 adds request.reopened; cancelled registry entry remains present.
    assert "request.reopened" in SUPPORTED_EVENT_TYPES
