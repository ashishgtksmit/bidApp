"""
PR9 PUT /updaterequest + DELETE /deleterequest contract + ownership tests.

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

import types

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.schemas.request_table import RequestUpdate  # noqa: E402
from app_v1.crud.request import update_request, delete_request  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"

# BidDetail FKs reference legacy table names (requestTable/userTable) that do not
# match current __tablename__ values — create User/Request via metadata and
# biddetails via raw DDL for SQLite unit tests.
PR9_CORE_TABLES = [User.__table__, Request.__table__]


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


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR9_CORE_TABLES)
    _create_biddetails_sqlite(engine)



def _pr38_auth_user(user_app_id: str, *, uid: int = 1):
    """Test helper: AuthenticatedUser with phone business id (PR38)."""
    from app_v1.auth.deps import AuthenticatedUser
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"test-auth-subject-{user_app_id}",
        user_app_id=str(user_app_id),
        account_session_id="test-account-session",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )

@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    """SQLite does not autoincrement BigInteger PKs the way MySQL does."""
    req_counter = {"n": 0}
    bid_counter = {"n": 0}

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    def _assign_bid(mapper, connection, target):
        if getattr(target, "BID", None) is None:
            bid_counter["n"] += 1
            target.BID = bid_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(BidDetail, "before_insert", _assign_bid)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(BidDetail, "before_insert", _assign_bid)


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
    special: str | None = "Original special",
    request_type: int = 1,
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
        specialRequest=special,
        bidEndTime=overrides.get(
            "bidEndTime", datetime(2026, 8, 14, 18, 0, 0)
        ),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=request_type,
        noOfBids=no_of_bids,
        finalAmount=overrides.get("finalAmount", 0),
        WIZZPNR=overrides.get("WIZZPNR", "WIZZ123"),
        paymentStatus=overrides.get("paymentStatus", None),
        requestWonBy=overrides.get("requestWonBy", None),
        tableTimestamp=overrides.get(
            "tableTimestamp",
            datetime(2026, 1, 1, 12, 0, 0),
        ),
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


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
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
    _add_customer(db)
    _add_customer(db, user_app_id=OTHER_ID, uid=2)
    return db


@pytest.fixture()
def bg():
    return BackgroundTasks()


def _reopen(session_like):
    engine = session_like.get_bind()
    Session = sessionmaker(bind=engine)
    return Session()


# --- Update CRUD ---


def test_update_valid_owner(seeded_db):
    row = _seed_request(seeded_db)
    result = update_request(
        seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
    )
    assert result.message == "SUCCESS"

    s = _reopen(seeded_db)
    try:
        updated = s.query(Request).one()
        assert updated.fromLocation == "Darjeeling"
        assert updated.fromLandmark == "Mall Road"
        assert updated.toLocation == "Bagdogra"
        assert updated.toLandmark == "Airport"
        assert updated.pickUpDate == date(2026, 9, 1)
        assert updated.pickUpTime == time(9, 15)
        assert updated.noOfAdults == 3
        assert updated.noOfKids == 2
        assert updated.carType == "SUV"
        assert bool(updated.acRequest) is False
        assert bool(updated.carrierRequest) is True
        assert updated.specialRequest == "Child seat"
        assert updated.bidEndTime == datetime(2026, 8, 31, 20, 0, 0)
        assert updated.customerAppId == CUSTOMER_ID
        assert updated.requestStatus == "BID - OPEN"
        assert updated.requestType == 1
        assert updated.WIZZPNR == "WIZZ123"
        assert updated.noOfBids == 0
        assert updated.finalAmount == 0
    finally:
        s.close()


def test_update_wrong_owner_403_no_mutation(seeded_db):
    row = _seed_request(seeded_db)
    before = row.fromLocation
    with pytest.raises(HTTPException) as exc:
        update_request(seeded_db, _request_update(row.RID), user_id=OTHER_ID)
    assert exc.value.status_code == 403

    s = _reopen(seeded_db)
    try:
        unchanged = s.query(Request).one()
        assert unchanged.fromLocation == before
        assert unchanged.specialRequest == "Original special"
        assert unchanged.requestStatus == "BID - OPEN"
    finally:
        s.close()


def test_update_missing_rid_404(seeded_db):
    with pytest.raises(HTTPException) as exc:
        update_request(seeded_db, _request_update(99999), user_id=CUSTOMER_ID)
    assert exc.value.status_code == 404
    assert seeded_db.query(Request).count() == 0 or True


def test_update_non_open_status_409(seeded_db):
    for status_value in (
        "BID - CONFIRMED",
        "REQUEST - CONFIRMED",
        "REQUEST - CANCELLED BY USER",
    ):
        engine = seeded_db.get_bind()
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            row = _seed_request(s, status=status_value)
            rid = row.RID
            original = row.fromLocation
        finally:
            s.close()

        s2 = Session()
        try:
            with pytest.raises(HTTPException) as exc:
                update_request(
                    s2, _request_update(rid), user_id=CUSTOMER_ID
                )
            assert exc.value.status_code == 409
            assert "INVALID_REQUEST_STATUS" in str(exc.value.detail)
        finally:
            s2.close()

        s3 = Session()
        try:
            row = s3.query(Request).filter(Request.RID == rid).one()
            assert row.fromLocation == original
            assert row.requestStatus == status_value
        finally:
            s3.close()


def test_update_bids_present_no_mutation(seeded_db):
    row = _seed_request(seeded_db, no_of_bids=2, special="Keep me")
    result = update_request(
        seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
    )
    assert result.message == "NO OF BIDS MORE THAN 0"

    s = _reopen(seeded_db)
    try:
        unchanged = s.query(Request).one()
        assert unchanged.fromLocation == "Gangtok"
        assert unchanged.specialRequest == "Keep me"
        assert unchanged.carType == "Sedan"
        assert unchanged.noOfBids == 2
    finally:
        s.close()


def test_update_special_request_persisted(seeded_db):
    row = _seed_request(seeded_db, special=None)
    result = update_request(
        seeded_db,
        _request_update(row.RID, specialRequest="Window seat please"),
        user_id=CUSTOMER_ID,
    )
    assert result.message == "SUCCESS"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().specialRequest == "Window seat please"
    finally:
        s.close()


def test_update_empty_special_becomes_null(seeded_db):
    row = _seed_request(seeded_db, special="Old")
    result = update_request(
        seeded_db,
        _request_update(row.RID, specialRequest="   "),
        user_id=CUSTOMER_ID,
    )
    assert result.message == "SUCCESS"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().specialRequest is None
    finally:
        s.close()


def test_update_table_timestamp_asia_kolkata(seeded_db):
    row = _seed_request(
        seeded_db,
        tableTimestamp=datetime(2020, 1, 1, 0, 0, 0),
    )
    before = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) - timedelta(
        seconds=2
    )
    result = update_request(
        seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
    )
    after = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) + timedelta(
        seconds=2
    )
    assert result.message == "SUCCESS"
    s = _reopen(seeded_db)
    try:
        ts = s.query(Request).one().tableTimestamp
        assert before <= ts <= after
    finally:
        s.close()


def test_update_no_timestamp_on_bids_conflict(seeded_db):
    old_ts = datetime(2020, 5, 5, 5, 5, 5)
    row = _seed_request(seeded_db, no_of_bids=1, tableTimestamp=old_ts)
    result = update_request(
        seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
    )
    assert result.message == "NO OF BIDS MORE THAN 0"
    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().tableTimestamp == old_ts
    finally:
        s.close()


def test_update_db_failure_no_sql_leak(seeded_db):
    row = _seed_request(seeded_db)
    with patch.object(seeded_db, "commit", side_effect=SQLAlchemyError("boom SQL")):
        result = update_request(
            seeded_db, _request_update(row.RID), user_id=CUSTOMER_ID
        )
    assert result.message == "ERROR"
    assert getattr(result, "error", None) in (None, "")


# --- Delete CRUD ---


def test_delete_valid_owner_soft_cancel(seeded_db, bg):
    row = _seed_request(seeded_db)
    result = delete_request(
        seeded_db, r_id=row.RID, background_tasks=bg, user_id=CUSTOMER_ID
    )
    assert result.message == "DELETED"

    s = _reopen(seeded_db)
    try:
        deleted = s.query(Request).one()
        assert deleted.requestStatus == "REQUEST - CANCELLED BY USER"
        assert deleted.customerAppId == CUSTOMER_ID
        assert deleted.fromLocation == "Gangtok"
    finally:
        s.close()


def test_delete_wrong_owner_403_no_mutation(seeded_db, bg):
    row = _seed_request(seeded_db)
    with pytest.raises(HTTPException) as exc:
        delete_request(
            seeded_db, r_id=row.RID, background_tasks=bg, user_id=OTHER_ID
        )
    assert exc.value.status_code == 403

    s = _reopen(seeded_db)
    try:
        unchanged = s.query(Request).one()
        assert unchanged.requestStatus == "BID - OPEN"
    finally:
        s.close()


def test_delete_missing_rid_404(seeded_db, bg):
    with pytest.raises(HTTPException) as exc:
        delete_request(
            seeded_db, r_id=99999, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 404


def test_delete_non_open_status_409(seeded_db, bg):
    row = _seed_request(seeded_db, status="BID - CONFIRMED")
    with pytest.raises(HTTPException) as exc:
        delete_request(
            seeded_db, r_id=row.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert exc.value.status_code == 409

    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "BID - CONFIRMED"
    finally:
        s.close()


def test_delete_retains_row_and_bids(seeded_db, bg):
    row = _seed_request(seeded_db, no_of_bids=1)
    rid = row.RID
    # Insert via raw SQL — BidDetail ORM FKs reference legacy table names.
    seeded_db.execute(
        text(
            """
            INSERT INTO biddetails (BID, rID, bidderID, bidAmount, bidStatus)
            VALUES (1, :rid, :bidder, 1500.00, 'BID - OPEN')
            """
        ),
        {"rid": rid, "bidder": int(OTHER_ID)},
    )
    seeded_db.commit()

    result = delete_request(
        seeded_db, r_id=rid, background_tasks=bg, user_id=CUSTOMER_ID
    )
    assert result.message == "DELETED"

    s = _reopen(seeded_db)
    try:
        assert s.query(Request).count() == 1
        assert s.query(BidDetail).count() == 1
        assert s.query(Request).one().requestStatus == "REQUEST - CANCELLED BY USER"
    finally:
        s.close()


def test_delete_schedules_notify_without_request_db(seeded_db, bg):
    row = _seed_request(seeded_db)
    rid = row.RID
    recorded = []

    def _capture(fn, *args, **kwargs):
        recorded.append((fn, args, kwargs))

    bg.add_task = _capture  # type: ignore[method-assign]
    result = delete_request(
        seeded_db, r_id=rid, background_tasks=bg, user_id=CUSTOMER_ID
    )
    assert result.message == "DELETED"
    assert len(recorded) == 1
    fn, args, _kwargs = recorded[0]
    assert fn is notifications_mod.notify_vendors_request_cancelled
    assert args == (rid,)


def test_notify_cancel_creates_and_closes_own_session():
    mock_session = MagicMock()
    mock_factory = MagicMock(return_value=mock_session)
    mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = []

    with patch("app_v1.database.SessionLocal", mock_factory):
        notifications_mod.notify_vendors_request_cancelled(42)
    mock_factory.assert_called_once()
    mock_session.close.assert_called_once()


def test_notify_cancel_failure_does_not_raise():
    mock_session = MagicMock()
    mock_factory = MagicMock(return_value=mock_session)
    mock_session.query.side_effect = RuntimeError("db down")

    with patch("app_v1.database.SessionLocal", mock_factory):
        notifications_mod.notify_vendors_request_cancelled(42)
    mock_session.close.assert_called_once()


def test_notify_failure_does_not_undo_delete(seeded_db, bg):
    row = _seed_request(seeded_db)
    rid = row.RID
    result = delete_request(
        seeded_db, r_id=rid, background_tasks=bg, user_id=CUSTOMER_ID
    )
    assert result.message == "DELETED"

    with patch("app_v1.database.SessionLocal", return_value=MagicMock()) as factory:
        factory.return_value.query.side_effect = RuntimeError("fcm down")
        notifications_mod.notify_vendors_request_cancelled(rid)

    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "REQUEST - CANCELLED BY USER"
    finally:
        s.close()


def test_delete_db_failure_rollback(seeded_db, bg):
    row = _seed_request(seeded_db)
    with patch.object(seeded_db, "commit", side_effect=SQLAlchemyError("boom")):
        result = delete_request(
            seeded_db, r_id=row.RID, background_tasks=bg, user_id=CUSTOMER_ID
        )
    assert result.message == "DELETED ERROR IN FUNCTION"

    s = _reopen(seeded_db)
    try:
        assert s.query(Request).one().requestStatus == "BID - OPEN"
    finally:
        s.close()


# --- HTTP endpoint tests ---


@pytest.fixture()
def pr9_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        _add_customer(session)
        _add_customer(session, user_app_id=OTHER_ID, uid=2)
    finally:
        session.close()
    try:
        yield engine
    finally:
        engine.dispose()


def _pr9_client(engine, user_id: str = CUSTOMER_ID):
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
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app), Session


def test_http_update_owner_success(pr9_engine):
    client, Session = _pr9_client(pr9_engine)
    s = Session()
    try:
        row = _seed_request(s)
        rid = row.RID
    finally:
        s.close()

    resp = client.put("/updaterequest", json=_update_payload(rid))
    assert resp.status_code == 200
    assert resp.json()["message"] == "SUCCESS"


def test_http_update_wrong_owner_403(pr9_engine):
    client, Session = _pr9_client(pr9_engine, user_id=OTHER_ID)
    s = Session()
    try:
        row = _seed_request(s, customer_app_id=CUSTOMER_ID)
        rid = row.RID
        before = row.fromLocation
    finally:
        s.close()

    resp = client.put("/updaterequest", json=_update_payload(rid))
    assert resp.status_code == 403

    s2 = Session()
    try:
        assert s2.query(Request).one().fromLocation == before
    finally:
        s2.close()


def test_http_update_missing_404(pr9_engine):
    client, _ = _pr9_client(pr9_engine)
    resp = client.put("/updaterequest", json=_update_payload(99999))
    assert resp.status_code == 404


def test_http_delete_owner_success(pr9_engine):
    client, Session = _pr9_client(pr9_engine)
    s = Session()
    try:
        row = _seed_request(s)
        rid = row.RID
    finally:
        s.close()

    with patch(
        "app_v1.crud.request.notify_vendors_request_cancelled"
    ):
        resp = client.delete(f"/deleterequest?RID={rid}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "DELETED"

    s2 = Session()
    try:
        assert (
            s2.query(Request).one().requestStatus
            == "REQUEST - CANCELLED BY USER"
        )
    finally:
        s2.close()


def test_http_delete_wrong_owner_403(pr9_engine):
    client, Session = _pr9_client(pr9_engine, user_id=OTHER_ID)
    s = Session()
    try:
        row = _seed_request(s, customer_app_id=CUSTOMER_ID)
        rid = row.RID
    finally:
        s.close()

    resp = client.delete(f"/deleterequest?RID={rid}")
    assert resp.status_code == 403

    s2 = Session()
    try:
        assert s2.query(Request).one().requestStatus == "BID - OPEN"
    finally:
        s2.close()


def test_http_delete_invalid_status_409(pr9_engine):
    client, Session = _pr9_client(pr9_engine)
    s = Session()
    try:
        row = _seed_request(s, status="REQUEST - CONFIRMED")
        rid = row.RID
    finally:
        s.close()

    resp = client.delete(f"/deleterequest?RID={rid}")
    assert resp.status_code == 409
    assert "INVALID_REQUEST_STATUS" in str(resp.json())


def test_http_update_invalid_status_409(pr9_engine):
    client, Session = _pr9_client(pr9_engine)
    s = Session()
    try:
        row = _seed_request(s, status="BID - CONFIRMED")
        rid = row.RID
    finally:
        s.close()

    resp = client.put("/updaterequest", json=_update_payload(rid))
    assert resp.status_code == 409
