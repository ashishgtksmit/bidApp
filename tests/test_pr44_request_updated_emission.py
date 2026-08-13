"""
PR44 request.updated FastAPI emission — transactional outbox on PUT /updaterequest.

All flags default false. Production enablement is not claimed here.
No FCM on update path (existing PR9 contract preserved).
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
from fastapi import HTTPException
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
    EVENT_REQUEST_UPDATED,
    EVENT_TYPE_FLAG_ENV,
)
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.schemas.request_table import RequestUpdate  # noqa: E402
from app_v1.crud.request import update_request  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"

PR44_TABLES = [User.__table__, Request.__table__, DomainOutboxEvent.__table__]

FLAG_REQUEST_UPDATED = "DOMAIN_EVENT_REQUEST_UPDATED_ENABLED"


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
    monkeypatch.delenv(FLAG_REQUEST_UPDATED, raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_REQUEST_CREATED_ENABLED", raising=False)
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
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _update_payload(rid: int, **overrides) -> dict:
    body = {
        "RID": rid,
        "fromLocation": "Darjeeling",
        "fromLandmark": "Mall Road",
        "toLocation": "Bagdogra",
        "toLandmark": "Airport",
        "pickUpDate": "2026-09-01",
        "pickUpTime": "09:15",
        "noOfAdults": 3,
        "noOfKids": 2,
        "carType": "SUV",
        "acRequest": False,
        "carrierRequest": True,
        "specialRequest": "Child seat",
        "bidEndTime": "2026-08-31 20:00:00",
    }
    body.update(overrides)
    return body


def _request_update(rid: int, **overrides) -> RequestUpdate:
    return RequestUpdate(**_update_payload(rid, **overrides))


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


def _enable_request_updated(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_UPDATED, "true")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR44_TABLES)
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


def _update_ok(session, rid: int, *, user_id: str = CUSTOMER_ID, **overrides):
    return update_request(
        session, _request_update(rid, **overrides), user_id=user_id
    )


# --- Flag / registry defaults ---


def test_01_flag_absent_zero_event(seeded_db, monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_UPDATED, raising=False)
    row = _seed_request(seeded_db)
    result = _update_ok(seeded_db, row.RID)
    assert result.message == "SUCCESS"
    assert _outbox(seeded_db) == []
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().fromLocation == "Darjeeling"
    finally:
        s.close()


def test_02_flag_false_zero_event(seeded_db, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_UPDATED, "false")
    row = _seed_request(seeded_db)
    result = _update_ok(seeded_db, row.RID)
    assert result.message == "SUCCESS"
    assert _outbox(seeded_db) == []


def test_03_master_false_zero_event(seeded_db, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv(FLAG_REQUEST_UPDATED, "true")
    row = _seed_request(seeded_db)
    result = _update_ok(seeded_db, row.RID)
    assert result.message == "SUCCESS"
    assert _outbox(seeded_db) == []
    assert event_emission_enabled(EVENT_REQUEST_UPDATED) is False


def test_04_both_enabled_exactly_one_event(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    result = _update_ok(seeded_db, rid)
    assert result.message == "SUCCESS"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.eventType == EVENT_REQUEST_UPDATED
    assert ev.schemaVersion == 1
    assert ev.aggregateType == "request"
    assert ev.aggregateId == str(rid)
    assert set(ev.payload.keys()) == {"requestId"}
    assert ev.payload == {"requestId": rid}
    assert ev.status == "pending"


def test_05_06_07_payload_schema_aggregate_exact(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    assert _update_ok(seeded_db, rid).message == "SUCCESS"
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
        "fromlocation",
        "preference",
        "recipient",
    ):
        assert bad not in dumped
    assert CUSTOMER_ID not in json.dumps(ev.payload)


def test_08_authorization_failure_403_zero_event(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    with pytest.raises(HTTPException) as exc:
        _update_ok(seeded_db, row.RID, user_id=OTHER_ID)
    assert exc.value.status_code == 403
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().fromLocation == "Gangtok"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_09_missing_request_404_zero_event(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _update_ok(seeded_db, 999999)
    assert exc.value.status_code == 404
    assert _outbox(seeded_db) == []


def test_10_validation_failure_zero_event(seeded_db, monkeypatch):
    """Pydantic rejects invalid update body before CRUD; no outbox."""
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    with pytest.raises(Exception):
        RequestUpdate(RID=row.RID, noOfAdults="not-an-int")  # type: ignore[arg-type]
    assert _outbox(seeded_db) == []


def test_11_invalid_lifecycle_status_409_zero_event(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db, status="BID - CONFIRMED")
    with pytest.raises(HTTPException) as exc:
        _update_ok(seeded_db, row.RID)
    assert exc.value.status_code == 409
    assert _outbox(seeded_db) == []


def test_11b_bids_present_soft_block_zero_event(seeded_db, monkeypatch):
    """noOfBids > 0 returns soft message; no mutation; no event."""
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db, no_of_bids=2)
    result = _update_ok(seeded_db, row.RID)
    assert result.message == "NO OF BIDS MORE THAN 0"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().fromLocation == "Gangtok"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_12_outbox_failure_rolls_back_business_update(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    with patch(
        "app_v1.crud.request.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        result = update_request(
            seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
        )
    assert result.message == "ERROR"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().fromLocation == "Gangtok"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_13_no_fcm_on_rollback_and_success(seeded_db, monkeypatch):
    """Update path has no FCM — rollback and success both schedule zero FCM."""
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    with patch(
        "app_v1.crud.request.maybe_append_domain_event",
        side_effect=RuntimeError("outbox failed"),
    ):
        with patch(
            "app_v1.crud.request.notify_vendors_for_request"
        ) as notify:
            result = update_request(
                seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
            )
    assert result.message == "ERROR"
    notify.assert_not_called()

    row2 = _seed_request(seeded_db, fromLocation="Kalimpong")
    with patch(
        "app_v1.crud.request.notify_vendors_for_request"
    ) as notify2:
        assert (
            update_request(
                seeded_db, _request_update(row2.RID), user_id=CUSTOMER_ID
            ).message
            == "SUCCESS"
        )
    notify2.assert_not_called()
    assert len(_outbox(seeded_db)) == 1


def test_14_fcm_post_commit_ordering_n_a_no_fcm(seeded_db, monkeypatch):
    """Documented: PUT /updaterequest has no FCM; event still commits alone."""
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    assert _update_ok(seeded_db, row.RID).message == "SUCCESS"
    assert len(_outbox(seeded_db)) == 1


def test_15_same_value_update_still_emits(seeded_db, monkeypatch):
    """
    Existing PR9 semantics: update always writes fields + bumps tableTimestamp
    even when values are unchanged. Emission follows the successful write.
    """
    _enable_request_updated(monkeypatch)
    row = _seed_request(
        seeded_db,
        fromLocation="Darjeeling",
        fromLandmark="Mall Road",
        toLocation="Bagdogra",
        toLandmark="Airport",
        pickUpDate=date(2026, 9, 1),
        pickUpTime=time(9, 15),
        noOfAdults=3,
        noOfKids=2,
        carType="SUV",
        acRequest=False,
        carrierRequest=True,
        specialRequest="Child seat",
        bidEndTime=datetime(2026, 8, 31, 20, 0, 0),
    )
    before_ts = row.tableTimestamp
    result = _update_ok(seeded_db, row.RID)
    assert result.message == "SUCCESS"
    rows = _outbox(seeded_db)
    assert len(rows) == 1
    assert rows[0].eventType == EVENT_REQUEST_UPDATED
    s = _reopen(seeded_db)
    try:
        after = s.query(Request).one()
        assert after.tableTimestamp != before_ts
    finally:
        s.close()


def test_16_process_bound_flag_mapping(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv(FLAG_REQUEST_UPDATED, raising=False)
    assert EVENT_REQUEST_UPDATED == "request.updated"
    assert EVENT_TYPE_FLAG_ENV[EVENT_REQUEST_UPDATED] == FLAG_REQUEST_UPDATED
    assert env_flag_enabled(FLAG_REQUEST_UPDATED) is False
    assert event_emission_enabled(EVENT_REQUEST_UPDATED) is False
    snap = process_bound_flag_snapshot(reason="unit")
    assert "requestUpdated" in snap
    ru = snap["requestUpdated"]
    assert ru["eventType"] == "request.updated"
    assert ru["envFlag"] == FLAG_REQUEST_UPDATED
    assert ru["perEventEnabled"] is False
    assert ru["emissionEnabled"] is False

    _enable_request_updated(monkeypatch)
    snap2 = process_bound_flag_snapshot(reason="unit")
    assert snap2["requestUpdated"]["perEventEnabled"] is True
    assert snap2["requestUpdated"]["emissionEnabled"] is True
    assert snap2["perEvent"][EVENT_REQUEST_UPDATED] is True


def test_17_failed_transaction_zero_event(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    with patch.object(seeded_db, "commit", side_effect=SQLAlchemyError("boom")):
        result = update_request(
            seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
        )
    assert result.message == "ERROR"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().fromLocation == "Gangtok"
        assert s.query(DomainOutboxEvent).count() == 0
    finally:
        s.close()


def test_18_event_exactly_once_per_successful_update(seeded_db, monkeypatch):
    _enable_request_updated(monkeypatch)
    row = _seed_request(seeded_db)
    rid = int(row.RID)
    assert _update_ok(seeded_db, rid).message == "SUCCESS"
    assert len(_outbox(seeded_db)) == 1

    s = _reopen(seeded_db)
    try:
        assert _update_ok(s, rid, fromLocation="Kalimpong").message == "SUCCESS"
        assert s.query(DomainOutboxEvent).count() == 2
        types = {e.eventType for e in s.query(DomainOutboxEvent).all()}
        assert types == {EVENT_REQUEST_UPDATED}
    finally:
        s.close()


def test_request_created_flag_alone_does_not_emit_updated(seeded_db, monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_REQUEST_CREATED_ENABLED", "true")
    monkeypatch.setenv(FLAG_REQUEST_UPDATED, "false")
    row = _seed_request(seeded_db)
    assert _update_ok(seeded_db, row.RID).message == "SUCCESS"
    assert _outbox(seeded_db) == []
