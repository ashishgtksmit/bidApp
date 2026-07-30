"""
PR8 POST /insertrequest contract + ownership + notify session tests.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
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

import types

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
from app_v1.schemas.request_table import RequestCreate  # noqa: E402
from app_v1.crud.request import create_request  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"

PR8_TABLES = [User.__table__, Request.__table__]


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
    payload = _valid_create_payload(**overrides)
    return RequestCreate(**payload)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=PR8_TABLES)
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
    tasks = BackgroundTasks()
    return tasks


def test_create_request_valid_authenticated(seeded_db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert result.message == "INSERTED"
    # create_request closes the session; reopen for asserts
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        row = s.query(Request).one()
        assert row.customerAppId == CUSTOMER_ID
        assert row.requestStatus == "BID - OPEN"
        assert row.requestType == 1
        assert row.noOfAdults == 2
        assert row.noOfKids == 1
        assert row.acRequest is True
        assert row.carrierRequest is False
        assert row.pickUpDate == date(2026, 8, 15)
        assert row.pickUpTime == time(10, 30)
    finally:
        s.close()


def test_create_request_matching_customer_allowed(seeded_db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(customerAppId=CUSTOMER_ID),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert result.message == "INSERTED"


def test_create_request_mismatch_raises_403_no_row(seeded_db, bg):
    with pytest.raises(HTTPException) as exc:
        create_request(
            seeded_db,
            _request_create(customerAppId=OTHER_ID),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert exc.value.status_code == 403

    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).count() == 0
    finally:
        s.close()


def test_create_request_missing_body_customer_raises_403(seeded_db, bg):
    create_data = _request_create(customerAppId="")
    with pytest.raises(HTTPException) as exc:
        create_request(
            seeded_db,
            create_data,
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert exc.value.status_code == 403


def test_create_request_jwt_value_is_persisted(seeded_db, bg):
    """Body phone is ignored for persistence when JWT sub is provided — JWT wins."""
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(customerAppId=CUSTOMER_ID),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).one().customerAppId == CUSTOMER_ID
    finally:
        s.close()


def test_create_request_optional_fields_omitted(seeded_db, bg):
    payload = _valid_create_payload()
    del payload["bidEndTime"]
    # specialRequest omitted
    create_data = RequestCreate(**payload)
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db, create_data, bg, user_id=CUSTOMER_ID, notify=False
        )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        row = s.query(Request).one()
        assert row.specialRequest is None
        assert row.requestType == 1
    finally:
        s.close()


def test_create_request_empty_special_request_stored_null(seeded_db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(specialRequest=""),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).one().specialRequest is None
    finally:
        s.close()


def test_create_request_booleans_and_ints(seeded_db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(noOfAdults=3, noOfKids=0, acRequest=False, carrierRequest=True),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        row = s.query(Request).one()
        assert row.noOfAdults == 3
        assert row.noOfKids == 0
        assert row.acRequest is False
        assert row.carrierRequest is True
    finally:
        s.close()


def test_create_request_status_forced_bid_open(seeded_db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db,
            _request_create(requestStatus="SOMETHING ELSE"),
            bg,
            user_id=CUSTOMER_ID,
            notify=False,
        )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).one().requestStatus == "BID - OPEN"
    finally:
        s.close()


def test_create_request_type_defaults_to_1_when_omitted(seeded_db, bg):
    create_data = _request_create()
    assert create_data.requestType is None
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db, create_data, bg, user_id=CUSTOMER_ID, notify=False
        )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).one().requestType == 1
    finally:
        s.close()


def test_duplicate_open_request_rejected(seeded_db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        first = create_request(
            seeded_db, _request_create(), bg, user_id=CUSTOMER_ID, notify=False
        )
    assert first.message == "INSERTED"

    # Fresh session — previous create_request closed seeded_db
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
            second = create_request(
                s, _request_create(), bg, user_id=CUSTOMER_ID, notify=False
            )
        assert second.message == "REQUEST_ALREADY_PRESENT"
        assert s.query(Request).count() == 1
    finally:
        s.close()


def test_historical_non_open_does_not_block(seeded_db, bg):
    """Reopen semantics: cancelled/confirmed historical rows must not block a new open request."""
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        s.add(
            Request(
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
                requestStatus="REQUEST - CANCELLED BY USER",
                customerAppId=CUSTOMER_ID,
                requestType=1,
                tableTimestamp=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
            )
        )
        s.commit()
    finally:
        s.close()

    s2 = Session()
    try:
        with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
            result = create_request(
                s2, _request_create(), bg, user_id=CUSTOMER_ID, notify=False
            )
        assert result.message == "INSERTED"
        s3 = Session()
        try:
            assert s3.query(Request).count() == 2
            open_rows = (
                s3.query(Request).filter(Request.requestStatus == "BID - OPEN").count()
            )
            assert open_rows == 1
        finally:
            s3.close()
    finally:
        s2.close()


def test_customer_not_found(db, bg):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            db, _request_create(), bg, user_id=CUSTOMER_ID, notify=False
        )
    assert result.message == "CUSTOMER_NOT_FOUND"
    engine = db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).count() == 0
    finally:
        s.close()


def test_db_failure_rollback(seeded_db, bg):
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
    assert result.error is None  # do not leak internal exception strings

    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).count() == 0
    finally:
        s.close()


def test_table_timestamp_uses_asia_kolkata(seeded_db, bg):
    before = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) - timedelta(
        seconds=2
    )
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = create_request(
            seeded_db, _request_create(), bg, user_id=CUSTOMER_ID, notify=False
        )
    after = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(
        seconds=2
    )
    assert result.message == "INSERTED"
    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        ts = s.query(Request).one().tableTimestamp
        assert before <= ts <= after
    finally:
        s.close()


def test_notify_task_scheduled_with_vendor_ids(seeded_db, bg):
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
    assert len(recorded) == 1
    fn, args, _kwargs = recorded[0]
    assert fn is notifications_mod.notify_vendors_for_request
    assert args[0] == ["111", "222"]
    assert len(args) == 2  # vendor_ids, create_data — no request-scoped db


def test_notify_vendors_creates_and_closes_own_session():
    mock_session = MagicMock()
    mock_factory = MagicMock(return_value=mock_session)

    # notify_vendors_for_request imports SessionLocal from app_v1.database at call time
    with patch("app_v1.database.SessionLocal", mock_factory):
        with patch(
            "app_v1.services.notifications.send_notification_to_selected_users"
        ) as send_mock:
            notifications_mod.notify_vendors_for_request(
                ["111"], _request_create()
            )
            mock_factory.assert_called_once()
            send_mock.assert_called_once()
            mock_session.close.assert_called_once()


def test_notify_failure_does_not_raise_and_closes_session():
    mock_session = MagicMock()
    mock_factory = MagicMock(return_value=mock_session)

    with patch("app_v1.database.SessionLocal", mock_factory):
        with patch(
            "app_v1.services.notifications.send_notification_to_selected_users",
            side_effect=RuntimeError("fcm down"),
        ):
            # Should not raise — committed request must remain intact
            notifications_mod.notify_vendors_for_request(
                ["111"], _request_create()
            )
    mock_session.close.assert_called_once()


def test_notification_failure_does_not_rollback_request(seeded_db, bg):
    """Request commit succeeds even when notify task would fail later."""
    with patch(
        "app_v1.crud.request.get_vendors_for_request",
        return_value=["111"],
    ):
        result = create_request(
            seeded_db,
            _request_create(),
            bg,
            user_id=CUSTOMER_ID,
            notify=True,
        )
    assert result.message == "INSERTED"

    engine = seeded_db.get_bind()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        assert s.query(Request).count() == 1
    finally:
        s.close()

    # Simulate background task failure after commit
    with patch("app_v1.database.SessionLocal", return_value=MagicMock()):
        with patch(
            "app_v1.services.notifications.send_notification_to_selected_users",
            side_effect=RuntimeError("fcm down"),
        ):
            notifications_mod.notify_vendors_for_request(["111"], _request_create())

    s2 = Session()
    try:
        assert s2.query(Request).count() == 1
    finally:
        s2.close()


# --- HTTP endpoint tests ---


@pytest.fixture()
def insert_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR8_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        _add_customer(session)
    finally:
        session.close()
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_client(engine, user_id: str = CUSTOMER_ID):
    app = FastAPI()
    app.include_router(request_router)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    return app


def test_http_insertrequest_success(insert_engine):
    app = _insert_client(insert_engine)
    client = TestClient(app)
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        response = client.post("/insertrequest", json=_valid_create_payload())
    assert response.status_code == 200
    assert response.json() == {"message": "INSERTED", "error": None}
    assert "RID" not in response.json()


def test_http_insertrequest_ownership_mismatch_403(insert_engine):
    app = _insert_client(insert_engine, user_id=CUSTOMER_ID)
    client = TestClient(app)
    response = client.post(
        "/insertrequest",
        json=_valid_create_payload(customerAppId=OTHER_ID),
    )
    assert response.status_code == 403
    Session = sessionmaker(bind=insert_engine)
    s = Session()
    try:
        assert s.query(Request).count() == 0
    finally:
        s.close()


def test_http_insertrequest_missing_required_field_422(insert_engine):
    app = _insert_client(insert_engine)
    client = TestClient(app)
    payload = _valid_create_payload()
    del payload["fromLocation"]
    response = client.post("/insertrequest", json=payload)
    assert response.status_code == 422


def test_http_insertrequest_rejects_missing_jwt(insert_engine):
    app = FastAPI()
    app.include_router(request_router)
    Session = sessionmaker(bind=insert_engine, autoflush=False, autocommit=False)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    response = client.post("/insertrequest", json=_valid_create_payload())
    assert response.status_code in (401, 403)
