"""
PR43 request.created FastAPI emission — transactional outbox on create/insert.

All flags default false. Production enablement is not claimed here.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, event
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
    EVENT_REQUEST_CREATED,
    EVENT_TYPE_FLAG_ENV,
)
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.schemas.request_table import RequestCreate  # noqa: E402
from app_v1.crud import request as request_mod  # noqa: E402
from app_v1.crud.request import create_request, insert_request_row  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"

PR43_TABLES = [User.__table__, Request.__table__, DomainOutboxEvent.__table__]

FLAG_REQUEST_CREATED = "DOMAIN_EVENT_REQUEST_CREATED_ENABLED"


@pytest.fixture(autouse=True)
def _sqlite_assign_rid():
    """SQLite does not autoincrement BigInteger PKs the way MySQL does."""
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
    monkeypatch.delenv(FLAG_REQUEST_CREATED, raising=False)
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


def _valid_create_payload(**overrides) -> dict:
    body = {
        "fromLocation": "Gangtok",
        "fromLandmark": "MG Marg",
        "toLocation": "Siliguri",
        "toLandmark": "NJP",
        "pickUpDate": "2026-08-15",
        "pickUpTime": "10:30",
        "noOfAdults": 2,
        "noOfKids": 1,
        "carType": "Sedan",
        "acRequest": True,
        "carrierRequest": False,
        "bidEndTime": "2026-08-14 18:00:00",
        "customerAppId": CUSTOMER_ID,
    }
    body.update(overrides)
    return body


def _request_create(**overrides) -> RequestCreate:
    return RequestCreate(**_valid_create_payload(**overrides))


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


def _enable_request_created(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_CREATED, "true")


def _disable_all(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_CREATED, "false")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR43_TABLES)
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
    return BackgroundTasks()


def _create_ok(session, bg, *, notify: bool = False, **payload_overrides):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        return create_request(
            session,
            _request_create(**payload_overrides),
            bg,
            user_id=CUSTOMER_ID,
            notify=notify,
        )


# --- Flag / registry defaults ---


def test_request_created_flag_env_mapping():
    assert EVENT_REQUEST_CREATED == "request.created"
    assert EVENT_TYPE_FLAG_ENV[EVENT_REQUEST_CREATED] == FLAG_REQUEST_CREATED


def test_flags_default_false_fail_closed(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_CREATED, raising=False)
    assert env_flag_enabled("DOMAIN_EVENTS_ENABLED") is False
    assert env_flag_enabled(FLAG_REQUEST_CREATED) is False
    assert event_emission_enabled(EVENT_REQUEST_CREATED) is False


def test_malformed_flag_fails_closed(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "maybe")
    monkeypatch.setenv(FLAG_REQUEST_CREATED, "")
    assert env_flag_enabled("DOMAIN_EVENTS_ENABLED") is False
    assert event_emission_enabled(EVENT_REQUEST_CREATED) is False


def test_process_bound_flag_snapshot_request_created_default_false(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_CREATED, raising=False)
    snap = process_bound_flag_snapshot(reason="unit")
    assert "requestCreated" in snap
    rc = snap["requestCreated"]
    assert rc["eventType"] == "request.created"
    assert rc["envFlag"] == FLAG_REQUEST_CREATED
    assert rc["perEventEnabled"] is False
    assert rc["emissionEnabled"] is False
    assert snap["perEvent"].get(EVENT_REQUEST_CREATED) is False


def test_process_bound_flag_snapshot_request_created_when_enabled(monkeypatch):
    _enable_request_created(monkeypatch)
    monkeypatch.setenv("OPENBID_DEPLOY_REVISION", "testrev-pr43")
    snap = process_bound_flag_snapshot(reason="unit")
    rc = snap["requestCreated"]
    assert rc["perEventEnabled"] is True
    assert rc["emissionEnabled"] is True
    assert snap["DOMAIN_EVENTS_ENABLED"] is True
    assert snap["perEvent"][EVENT_REQUEST_CREATED] is True
    assert snap["deployRevision"] == "testrev-pr43"
    blob = str(snap).lower()
    assert "password" not in blob
    assert "fcm" not in blob


# --- Emission matrix ---


def test_flag_unset_successful_create_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_CREATED, raising=False)
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 1
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_flag_false_zero_event(seeded_db, bg, monkeypatch):
    _disable_all(monkeypatch)
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    assert _outbox(seeded_db) == []
    assert _reopen(seeded_db).query(Request).count() == 1


def test_master_false_request_created_true_zero_event(seeded_db, bg, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_CREATED, "true")
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    assert _outbox(seeded_db) == []
    assert event_emission_enabled(EVENT_REQUEST_CREATED) is False


def test_master_true_request_created_true_exactly_one_event(
    seeded_db, bg, monkeypatch
):
    _enable_request_created(monkeypatch)
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    s = _reopen(seeded_db)
    try:
        rid = s.query(Request).one().RID
    finally:
        s.close()
    ev = rows[0]
    assert ev.eventType == EVENT_REQUEST_CREATED
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(rid)
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.payload == {"requestId": rid}
    assert ev.status == "pending"


def test_payload_schema_aggregate_contract(seeded_db, bg, monkeypatch):
    """Payload exactly requestId; schemaVersion 1; aggregate request."""
    _enable_request_created(monkeypatch)
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    ev = _outbox(seeded_db)[0]
    rid = _reopen(seeded_db).query(Request).one().RID
    assert ev.payload == {"requestId": rid}
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(rid)
    dumped = json.dumps(ev.payload).lower()
    for bad in ("fcm", "token", "jwt", "password", "customerappid", "phone"):
        assert bad not in dumped
    assert CUSTOMER_ID not in json.dumps(ev.payload)


def test_outbox_same_transaction_after_successful_create(seeded_db, bg, monkeypatch):
    """Outbox row present in same DB after successful create (atomic commit)."""
    _enable_request_created(monkeypatch)
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 1
        assert s.query(DomainOutboxEvent).count() == 1
        rid = s.query(Request).one().RID
        ev = s.query(DomainOutboxEvent).one()
        assert ev.aggregateId == str(rid)
        assert ev.payload["requestId"] == rid
    finally:
        s.close()


def test_append_failure_rolls_back_request(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    with patch(
        "app_v1.crud.request.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
            result = create_request(
                seeded_db,
                _request_create(),
                bg,
                user_id=CUSTOMER_ID,
                notify=False,
            )
    assert result.message == "ERROR_INSERT"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 0
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_append_failure_no_fcm_side_effect(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    recorded = []
    bg.add_task = lambda fn, *a, **k: recorded.append((fn, a, k))  # type: ignore[method-assign]
    with patch(
        "app_v1.crud.request.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        with patch(
            "app_v1.crud.request.get_vendors_for_request",
            return_value=["111", "222"],
        ):
            result = create_request(
                seeded_db,
                _request_create(),
                bg,
                user_id=CUSTOMER_ID,
                notify=True,
            )
    assert result.message == "ERROR_INSERT"
    assert recorded == []
    assert bg.tasks == []
    assert _reopen(seeded_db).query(Request).count() == 0
    assert _outbox(seeded_db) == []


def test_successful_commit_fcm_still_scheduled(seeded_db, bg, monkeypatch):
    """Existing FCM path unchanged: notify still scheduled when notify=True."""
    _enable_request_created(monkeypatch)
    recorded = []

    def _capture(fn, *args, **kwargs):
        recorded.append((fn, args, kwargs))

    bg.add_task = _capture  # type: ignore[method-assign]
    with patch(
        "app_v1.crud.request.get_vendors_for_request",
        return_value=["111", "222"],
    ):
        result = create_request(
            seeded_db,
            _request_create(),
            bg,
            user_id=CUSTOMER_ID,
            notify=True,
        )
    assert result.message == "INSERTED"
    assert len(_outbox(seeded_db)) == 1
    assert len(recorded) == 1
    fn, args, _kwargs = recorded[0]
    assert fn is notifications_mod.notify_vendors_for_request
    assert args[0] == ["111", "222"]


def test_auth_failure_403_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        create_request(
            seeded_db,
            _request_create(customerAppId=OTHER_ID),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert exc.value.status_code == 403
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 0
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_validation_customer_not_found_zero_event(db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            db, _request_create(), bg, user_id=CUSTOMER_ID, notify=False
        )
    assert result.message == "CUSTOMER_NOT_FOUND"
    s = _reopen(db)
    try:
        assert s.query(Request).count() == 0
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_validation_request_already_present_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    first = _create_ok(seeded_db, bg)
    assert first.message == "INSERTED"
    assert len(_outbox(seeded_db)) == 1

    s = _reopen(seeded_db)
    try:
        second = _create_ok(s, BackgroundTasks())
        assert second.message == "REQUEST_ALREADY_PRESENT"
        assert s.query(Request).count() == 1
        assert s.query(DomainOutboxEvent).count() == 1
    finally:
        s.close()


def test_request_creation_db_failure_zero_event(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    with patch.object(seeded_db, "commit", side_effect=SQLAlchemyError("boom")):
        with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
            result = create_request(
                seeded_db,
                _request_create(),
                bg,
                user_id=CUSTOMER_ID,
                notify=False,
            )
    assert result.message == "ERROR_INSERT"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 0
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_reopen_commit_false_does_not_emit_request_created(seeded_db, monkeypatch):
    """Reopen nested path (commit=False) must not emit request.created."""
    _enable_request_created(monkeypatch)
    result = insert_request_row(
        seeded_db,
        _request_create(),
        user_id=CUSTOMER_ID,
        commit=False,
        close_session=False,
        notify=False,
        emit=True,
    )
    assert isinstance(result, Request)
    assert result.RID is not None
    seeded_db.commit()
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 1
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_emit_false_skips_append_even_when_flags_on(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
            emit=False,
        )
    assert result.message == "INSERTED"
    assert _outbox(seeded_db) == []
    assert _reopen(seeded_db).query(Request).count() == 1


def test_insert_request_row_source_order_append_before_commit():
    """maybe_append_domain_event must appear before db.commit in insert_request_row."""
    src = Path(request_mod.__file__).read_text()
    fn_start = src.index("def insert_request_row")
    fn_end = src.index("\ndef create_request", fn_start)
    fn = src[fn_start:fn_end]
    append_at = fn.index("maybe_append_domain_event")
    commit_at = fn.index("db.commit()")
    assert append_at < commit_at
    # Nested reopen path returns before append.
    early = fn[:append_at]
    assert "if not commit:" in early
    assert "return new_request" in early
    notify_at = fn.index("notify_vendors_for_request", commit_at)
    assert commit_at < notify_at


def test_both_flags_true_emit_true_envelope_fields(seeded_db, bg, monkeypatch):
    _enable_request_created(monkeypatch)
    result = _create_ok(seeded_db, bg)
    assert result.message == "INSERTED"
    rid = _reopen(seeded_db).query(Request).one().RID
    ev = _outbox(seeded_db)[0]
    assert ev.eventType == "request.created"
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(rid)
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.payload["requestId"] == rid
