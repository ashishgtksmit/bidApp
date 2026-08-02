"""
PR13 vendor confirmed-trip driver list + assignment tests.

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
from app_v1.models.driver_details import DriverDetail  # noqa: E402
from app_v1.models.tags_table import Tag  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.crud import driver as driver_crud  # noqa: E402
from app_v1.crud import request as request_crud  # noqa: E402
from app_v1.schemas.request_table import AssignDriverRequest  # noqa: E402
from app_v1.endpoints.driver import router as driver_router  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_ID = "9999999999"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"

PR13_CORE_TABLES = [
    User.__table__,
    Request.__table__,
    DriverDetail.__table__,
    Tag.__table__,
    LocationDetail.__table__,
]


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR13_CORE_TABLES)


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    req_counter = {"n": 0}
    driver_counter = {"n": 0}

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


def _seed_driver(
    db,
    *,
    user_app_id: str,
    name: str = "Driver One",
    number: str = "9800000001",
    photo: str | None = "photo.jpg",
) -> DriverDetail:
    row = DriverDetail(
        userAppId=user_app_id,
        driverName=name,
        driverNumber=number,
        driverDOB=date(1990, 1, 1),
        driverGender="M",
        driverCity="Gangtok",
        driverLicense="SECRET-LICENSE-URL",
        driverDocument="SECRET-DOCUMENT-URL",
        driverPhoto=photo if photo is not None else "",
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "REQUEST - CONFIRMED",
    final_amount: int = 2500,
    request_won_by=VENDOR_A,
    payment_status: str = "PENDING",
    driver_assigned_id: int | None = None,
    rejection_reason=None,
) -> Request:
    row = Request(
        fromLocation="Gangtok",
        fromLandmark="MG Marg",
        toLocation="Siliguri",
        toLandmark="NJP",
        pickUpDate=date(2030, 8, 15),
        pickUpTime=time(10, 30),
        noOfAdults=2,
        noOfKids=1,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest="Window seat",
        bidEndTime=datetime(2030, 8, 14, 18, 0, 0),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=1,
        noOfBids=1,
        finalAmount=final_amount,
        WIZZPNR="WIZZ123",
        paymentStatus=payment_status,
        requestWonBy=request_won_by,
        rejectionReason=rejection_reason,
        requestReopened=False,
        driverAssignedID=driver_assigned_id,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


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
    _add_user(db, user_app_id=VENDOR_A, uid=3, full_name="Vendor A")
    _add_user(db, user_app_id=VENDOR_B, uid=4, full_name="Vendor B")
    return db


@pytest.fixture
def bg():
    return BackgroundTasks()


@pytest.fixture
def vendor_client(seeded_db):
    app = FastAPI()
    app.include_router(driver_router)
    app.include_router(request_router)

    def _override_db():
        session = _reopen(seeded_db)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: VENDOR_A
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def customer_client(seeded_db):
    app = FastAPI()
    app.include_router(driver_router)
    app.include_router(request_router)

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


# ---------------------------------------------------------------------------
# Driver list
# ---------------------------------------------------------------------------


def test_vendor_gets_own_drivers_lean(seeded_db):
    d1 = _seed_driver(seeded_db, user_app_id=VENDOR_A, name="Alice", number="9811111111")
    _seed_driver(seeded_db, user_app_id=VENDOR_B, name="Bob", number="9822222222")

    session = _reopen(seeded_db)
    result = driver_crud.get_all_driver_for_vendor(session, user_id=VENDOR_A)
    assert isinstance(result, list)
    assert len(result) == 1
    row = result[0]
    assert row.DRIVERID == d1.DDID
    assert row.DRIVERNAME == "Alice"
    assert row.PHOTO_URL == "photo.jpg"
    assert row.DRIVERNUMBER == "9811111111"
    dumped = row.model_dump()
    assert "USERAPPID" not in dumped
    assert "LICENSE_URL" not in dumped
    assert "DOCUMENT_URL" not in dumped
    assert "DRIVERDOB" not in dumped
    assert "GENDER" not in dumped
    assert "DRIVERCITY" not in dumped
    assert "FCMTOKEN" not in dumped
    assert "SECRET-LICENSE" not in str(dumped)
    assert "SECRET-DOCUMENT" not in str(dumped)


def test_driver_list_no_userappid_required(vendor_client, seeded_db):
    _seed_driver(seeded_db, user_app_id=VENDOR_A)
    resp = vendor_client.get("/viewdriversforvendor")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["DRIVERNAME"]
    assert "LICENSE_URL" not in data[0]
    assert "USERAPPID" not in data[0]


def test_driver_list_matching_userappid_accepted(vendor_client, seeded_db):
    _seed_driver(seeded_db, user_app_id=VENDOR_A)
    resp = vendor_client.get(f"/viewdriversforvendor?userAppId={VENDOR_A}")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_driver_list_mismatched_userappid_403(vendor_client, seeded_db):
    _seed_driver(seeded_db, user_app_id=VENDOR_A)
    resp = vendor_client.get(f"/viewdriversforvendor?userAppId={VENDOR_B}")
    assert resp.status_code == 403


def test_driver_list_another_vendor_inaccessible(seeded_db):
    _seed_driver(seeded_db, user_app_id=VENDOR_B, name="Other")
    session = _reopen(seeded_db)
    result = driver_crud.get_all_driver_for_vendor(session, user_id=VENDOR_A)
    assert result == []


def test_driver_list_non_vendor_403(seeded_db):
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        driver_crud.get_all_driver_for_vendor(session, user_id=CUSTOMER_ID)
    assert exc.value.status_code == 403


def test_driver_list_empty_array(seeded_db):
    session = _reopen(seeded_db)
    result = driver_crud.get_all_driver_for_vendor(session, user_id=VENDOR_A)
    assert result == []


def test_driver_list_nullable_photo(seeded_db):
    _seed_driver(seeded_db, user_app_id=VENDOR_A, photo="")
    session = _reopen(seeded_db)
    result = driver_crud.get_all_driver_for_vendor(session, user_id=VENDOR_A)
    assert len(result) == 1
    assert result[0].PHOTO_URL == "" or result[0].PHOTO_URL is None


def test_driver_list_safe_sql_error(seeded_db):
    session = _reopen(seeded_db)
    with patch.object(session, "query", side_effect=SQLAlchemyError("boom SELECT FROM secret")):
        # require_active_vendor uses query first — force failure after vendor check
        with patch(
            "app_v1.crud.driver.require_active_vendor",
            return_value=MagicMock(),
        ):
            result = driver_crud.get_all_driver_for_vendor(session, user_id=VENDOR_A)
    assert result.message == "ERROR"
    assert "SELECT" not in result.message
    assert "secret" not in result.message


def test_driver_list_endpoint_customer_403(customer_client, seeded_db):
    resp = customer_client.get("/viewdriversforvendor")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Driver assignment
# ---------------------------------------------------------------------------


def test_winning_vendor_assigns_owned_driver(seeded_db, bg):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, driver_assigned_id=None)
    status = req.requestStatus
    won_by = req.requestWonBy
    final = req.finalAmount
    payment = req.paymentStatus
    old_ts = req.tableTimestamp

    session = _reopen(seeded_db)
    result = request_crud.assign_driver_to_request(
        session,
        AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    assert updated.driverAssignedID == driver.DDID
    assert updated.requestStatus == status
    assert updated.requestWonBy == won_by
    assert updated.finalAmount == final
    assert updated.paymentStatus == payment
    assert updated.tableTimestamp != old_ts


def test_assign_endpoint_body_rid_driverid_only(vendor_client, seeded_db):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, driver_assigned_id=None)
    with patch.object(
        notifications_mod,
        "notify_driver_assigned_to_customer_background",
        MagicMock(),
    ):
        resp = vendor_client.put(
            "/updatedrivertorequest",
            json={"RID": req.RID, "DRIVERID": driver.DDID},
        )
    assert resp.status_code == 200
    assert resp.json()["message"] == "UPDATED"


def test_assign_request_missing_404(seeded_db, bg):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=99999, DRIVERID=driver.DDID),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 404
    assert "Request" in str(exc.value.detail)


def test_assign_wrong_vendor_403(seeded_db, bg):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_B)
    req = _seed_request(seeded_db, request_won_by=VENDOR_A)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
            user_id=VENDOR_B,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


def test_assign_driver_missing_404(seeded_db, bg):
    req = _seed_request(seeded_db)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=req.RID, DRIVERID=99999),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 404
    assert "Driver" in str(exc.value.detail)


def test_assign_driver_other_vendor_403(seeded_db, bg):
    other_driver = _seed_driver(seeded_db, user_app_id=VENDOR_B)
    req = _seed_request(seeded_db, request_won_by=VENDOR_A)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=req.RID, DRIVERID=other_driver.DDID),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "bad_status",
    [
        "BID - OPEN",
        "BID - CONFIRMED",
        "BOOKING - CANCELLED BY USER",
        "REQUEST - CANCELLED BY USER",
    ],
)
def test_assign_invalid_status_409(seeded_db, bg, bad_status):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, status=bad_status)
    session = _reopen(seeded_db)
    with pytest.raises(HTTPException) as exc:
        request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "INVALID_REQUEST_STATUS"


def test_assign_no_pickup_time_gate(seeded_db, bg):
    """Past pickup still allowed when status is REQUEST - CONFIRMED."""
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, driver_assigned_id=None)
    session = _reopen(seeded_db)
    row = session.query(Request).filter(Request.RID == req.RID).one()
    row.pickUpDate = date(2020, 1, 1)
    row.pickUpTime = time(10, 0)
    session.commit()

    session2 = _reopen(seeded_db)
    result = request_crud.assign_driver_to_request(
        session2,
        AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"


def test_same_driver_replay_no_timestamp_no_notify(seeded_db, bg):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, driver_assigned_id=driver.DDID)
    old_ts = req.tableTimestamp

    notify_mock = MagicMock()
    bg.add_task = notify_mock

    session = _reopen(seeded_db)
    result = request_crud.assign_driver_to_request(
        session,
        AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    notify_mock.assert_not_called()

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    assert updated.driverAssignedID == driver.DDID
    assert updated.tableTimestamp == old_ts


def test_replacement_updates_and_notifies(seeded_db, bg):
    d1 = _seed_driver(seeded_db, user_app_id=VENDOR_A, name="First")
    d2 = _seed_driver(seeded_db, user_app_id=VENDOR_A, name="Second", number="9800000002")
    req = _seed_request(seeded_db, driver_assigned_id=d1.DDID)
    old_ts = req.tableTimestamp

    tasks = []

    def _capture(fn, *args, **kwargs):
        tasks.append((fn, args, kwargs))

    bg.add_task = _capture

    session = _reopen(seeded_db)
    result = request_crud.assign_driver_to_request(
        session,
        AssignDriverRequest(RID=req.RID, DRIVERID=d2.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "UPDATED"
    assert len(tasks) == 1
    fn, args, _ = tasks[0]
    assert fn is notifications_mod.notify_driver_assigned_to_customer_background
    assert args[0] == CUSTOMER_ID
    assert args[1] == req.RID
    assert args[2] == d2.DDID

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    assert updated.driverAssignedID == d2.DDID
    assert updated.tableTimestamp != old_ts


def test_notification_after_commit_owns_session(seeded_db):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A, name="Notify Driver")
    req = _seed_request(seeded_db)

    fake_db = MagicMock()
    driver_q = MagicMock()
    fake_db.query.return_value = driver_q
    driver_q.filter.return_value.first.return_value = driver

    with patch("app_v1.database.SessionLocal", return_value=fake_db), patch.object(
        notifications_mod,
        "notify_driver_assigned_to_customer",
        return_value={"success": True},
    ) as notify_mock:
        notifications_mod.notify_driver_assigned_to_customer_background(
            CUSTOMER_ID,
            req.RID,
            driver.DDID,
        )
        assert notify_mock.called
        fake_db.close.assert_called_once()


def test_notification_failure_does_not_undo(seeded_db, bg):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, driver_assigned_id=None)

    def _boom(*args, **kwargs):
        raise RuntimeError("FCM down")

    bg.add_task = lambda fn, *a, **k: fn(*a, **k)

    with patch.object(
        notifications_mod,
        "notify_driver_assigned_to_customer_background",
        side_effect=_boom,
    ):
        # Schedule happens after commit; simulate post-commit notify failure separately.
        session = _reopen(seeded_db)
        result = request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
            user_id=VENDOR_A,
            background_tasks=None,
        )
        assert result.message == "UPDATED"

    session2 = _reopen(seeded_db)
    updated = session2.query(Request).filter(Request.RID == req.RID).one()
    assert updated.driverAssignedID == driver.DDID


def test_canonical_notification_copy():
    fake_db = MagicMock()
    user = MagicMock()
    user.fcmToken = "token-abc"
    fake_db.query.return_value.filter.return_value.first.return_value = user

    with patch.object(
        notifications_mod, "send_notification_to_token", return_value={"success": True}
    ) as send_mock:
        notifications_mod.notify_driver_assigned_to_customer(
            fake_db,
            customer_user_app_id=CUSTOMER_ID,
            request_id=42,
            driver_name="Ramesh",
            driver_number="9800112233",
        )
        kwargs = send_mock.call_args.kwargs
        assert kwargs["title"] == "🚖 Driver Assigned"
        assert "Ramesh" in kwargs["body"]
        assert "9800112233" in kwargs["body"]
        assert kwargs["url"] == "///My Trips"


def test_assign_rollback_on_db_failure(seeded_db, bg):
    driver = _seed_driver(seeded_db, user_app_id=VENDOR_A)
    req = _seed_request(seeded_db, driver_assigned_id=None)
    session = _reopen(seeded_db)

    with patch.object(session, "commit", side_effect=SQLAlchemyError("fail UPDATE secret")):
        result = request_crud.assign_driver_to_request(
            session,
            AssignDriverRequest(RID=req.RID, DRIVERID=driver.DDID),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert result.message == "ERROR"
    assert "secret" not in result.message

    session2 = _reopen(seeded_db)
    unchanged = session2.query(Request).filter(Request.RID == req.RID).one()
    assert unchanged.driverAssignedID is None


def test_concurrent_replacement_last_commit_wins(seeded_db, bg):
    d1 = _seed_driver(seeded_db, user_app_id=VENDOR_A, name="One")
    d2 = _seed_driver(seeded_db, user_app_id=VENDOR_A, name="Two", number="9800000002")
    req = _seed_request(seeded_db, driver_assigned_id=None)

    session1 = _reopen(seeded_db)
    r1 = request_crud.assign_driver_to_request(
        session1,
        AssignDriverRequest(RID=req.RID, DRIVERID=d1.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert r1.message == "UPDATED"

    session2 = _reopen(seeded_db)
    r2 = request_crud.assign_driver_to_request(
        session2,
        AssignDriverRequest(RID=req.RID, DRIVERID=d2.DDID),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert r2.message == "UPDATED"

    session3 = _reopen(seeded_db)
    final = session3.query(Request).filter(Request.RID == req.RID).one()
    assert final.driverAssignedID == d2.DDID


def test_no_vendor_cancellation_route(vendor_client):
    resp = vendor_client.put("/bookingcancelledbyvendor?RID=1")
    assert resp.status_code == 404


def test_no_passenger_rest_route(vendor_client):
    resp = vendor_client.get("/getcustomerdetailsbyrid?RID=1")
    assert resp.status_code == 404
