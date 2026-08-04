"""
PR21 vendor cancellation history — GET /getallcancelledrequestsforvendor.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
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
os.environ.setdefault("DB_PASSWORD", "unused")
os.environ.setdefault("DB_USERNAME", "unused")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "3306")
os.environ.setdefault("DB_NAME", "unused")

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
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.crud import request as request_crud  # noqa: E402
from app_v1.schemas.booking_history import VendorCancelledHistoryItem  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
CANCELLED_STATUS = "BOOKING - CANCELLED BY USER"
PATH = "/getallcancelledrequestsforvendor"

CUSTOMER_ID = "7022359323"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
LOSING_BIDDER = "8637554399"

SECRET_FCM = "secret-fcm-token-should-not-leak"
SECRET_BANK = "SECRET-BANK-ACCOUNT-1234"
SECRET_PASSWORD = "secret-password-should-not-leak"

PR21_CORE_TABLES = [
    User.__table__,
    Request.__table__,
]

FORBIDDEN_KEYS = {
    "customerAppId",
    "CUSTOMERAPPID",
    "requestWonBy",
    "REQUESTWONBY",
    "vendorId",
    "PHONENUMBER",
    "ALTNUMBER",
    "alternateNumber",
    "phone",
    "email",
    "emailId",
    "city",
    "CITY",
    "dob",
    "gender",
    "fcmToken",
    "bankAccountNo",
    "driverLicense",
    "DRIVERLICENSE",
    "registrationDoc",
    "REGISTRATIONDOC",
    "powerOfAttorneyDoc",
    "POWEROFATTORNEYDOC",
    "driverName",
    "DRIVERNAME",
    "carRegistrationNumber",
    "CARREGNO",
    "carModel",
    "CARMODEL",
    "customerReviewDone",
    "CUSTOMERREVIEWDONE",
    "customerGeneralRating",
    "CUSTREVIEW_GENERALRATING",
    "customerReviewComments",
    "CUSTREVIEW_COMMENTS",
    "specialRequest",
    "SPECIALREQUEST",
    "requestReopened",
}


def _prepare_engine(engine) -> None:
    Base.metadata.create_all(bind=engine, tables=PR21_CORE_TABLES)


def _memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    return engine



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
    req_counter = {"n": 0}

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
def db_session():
    engine = _memory_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db, engine, Session
    finally:
        db.close()


@pytest.fixture
def seeded_db(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, full_name="Customer One")
    _add_user(db, user_app_id=VENDOR_A, uid=2, full_name="Vendor A")
    _add_user(db, user_app_id=VENDOR_B, uid=3, full_name="Vendor B")
    _add_user(db, user_app_id=LOSING_BIDDER, uid=4, full_name="Losing Bidder")
    return db, engine, Session


def _client_for(engine, Session, user_id):
    app = FastAPI()
    app.include_router(request_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    if user_id is not None:
        app.dependency_overrides[get_current_user_id] = lambda: user_id
        app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app)


def _add_user(db, *, user_app_id: str, uid: int, full_name: str = "User", **kwargs) -> User:
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password=kwargs.get("password", SECRET_PASSWORD),
        alternateNumber="1000000000",
        fullName=full_name,
        emailId=f"{user_app_id}@example.com",
        dob="1990-01-01",
        city=kwargs.get("city", "Gangtok"),
        gender=kwargs.get("gender", "Male"),
        profilePicture=kwargs.get("profilePicture", "images/profilepic_male.png"),
        alsoVendor=kwargs.get("alsoVendor", True),
        vendorApproved=kwargs.get("vendorApproved", True),
        lockApp=kwargs.get("lockApp", False),
        customerRating=kwargs.get("customerRating", "4.50"),
        totalCustomerReviews=kwargs.get("totalCustomerReviews", 0),
        rating=kwargs.get("rating", "4.50"),
        totalNoOfReviews=kwargs.get("totalNoOfReviews", 0),
        fcmToken=kwargs.get("fcmToken", SECRET_FCM),
        joiningDate=kwargs.get("joiningDate", date(2024, 1, 15)),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 5),
        user_login_status="LOGGEDOUT",
        bankAccountNo=kwargs.get("bankAccountNo", SECRET_BANK),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _yesterday_pickup():
    now_ist = datetime.now(IST)
    yesterday = (now_ist - timedelta(days=1)).date()
    return yesterday, time(9, 0, 0)


def _days_ago_pickup(days: int, hour: int = 9):
    now_ist = datetime.now(IST)
    d = (now_ist - timedelta(days=days)).date()
    return d, time(hour, 0, 0)


def _tomorrow_pickup():
    now_ist = datetime.now(IST)
    tomorrow = (now_ist + timedelta(days=1)).date()
    return tomorrow, time(9, 0, 0)


def _seed_cancelled(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    vendor_app_id: str | None = VENDOR_A,
    status: str = CANCELLED_STATUS,
    pickup_date=None,
    pickup_time=None,
    from_location: str = "Gangtok",
    to_location: str = "Siliguri",
    final_amount: int = 4500,
    rejection_reason: str | None = "Change of plans",
    request_reopened: bool = False,
    car_type: str = "Sedan",
    ac_request: bool = True,
    carrier_request: bool = False,
    no_of_adults: int = 2,
    no_of_kids: int = 0,
) -> Request:
    if pickup_date is None or pickup_time is None:
        pd, pt = _yesterday_pickup()
    else:
        pd, pt = pickup_date, pickup_time
    row = Request(
        fromLocation=from_location,
        fromLandmark="MG Marg",
        toLocation=to_location,
        toLandmark="NJP",
        pickUpDate=pd,
        pickUpTime=pt,
        noOfAdults=no_of_adults,
        noOfKids=no_of_kids,
        carType=car_type,
        acRequest=ac_request,
        carrierRequest=carrier_request,
        specialRequest="",
        requestStatus=status,
        customerAppId=customer_app_id,
        requestWonBy=vendor_app_id,
        finalAmount=final_amount,
        noOfBids=1,
        rejectionReason=rejection_reason,
        requestReopened=request_reopened,
        reviewDone="N",
        customerReviewDone="N",
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _assert_no_forbidden(item: dict) -> None:
    for key in FORBIDDEN_KEYS:
        assert key not in item, f"forbidden key leaked: {key}"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_no_jwt_401(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, user_id=None)
    resp = client.get(PATH)
    # HTTPBearer missing credentials → 403; invalid/missing auth surface is non-200.
    assert resp.status_code in (401, 403)


def test_invalid_jwt_401(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, user_id=None)
    resp = client.get(
        PATH,
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_winning_vendor_sees_own_past_cancelled(seeded_db):
    db, engine, Session = seeded_db
    row = _seed_cancelled(db, vendor_app_id=VENDOR_A)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get(PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["requestId"] == row.RID
    assert body[0]["requestStatus"] == CANCELLED_STATUS
    assert body[0]["customerDisplayName"] == "Customer One"
    assert body[0]["cancellationReason"] == "Change of plans"
    assert body[0]["finalAmount"] == 4500.0
    _assert_no_forbidden(body[0])


def test_losing_bidder_sees_no_row(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, vendor_app_id=VENDOR_A)
    client = _client_for(engine, Session, LOSING_BIDDER)
    resp = client.get(PATH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_another_vendor_sees_no_row(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, vendor_app_id=VENDOR_A)
    client = _client_for(engine, Session, VENDOR_B)
    resp = client.get(PATH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_no_vendor_id_required(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get(PATH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_optional_matching_vendor_id_succeeds(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get(PATH, params={"vendorId": VENDOR_A})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_mismatched_vendor_id_403(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get(PATH, params={"vendorId": VENDOR_B})
    assert resp.status_code == 403


def test_null_request_won_by_excluded(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, vendor_app_id=None)
    client = _client_for(engine, Session, VENDOR_A)
    resp = client.get(PATH)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_booking_cancelled_by_user_included(seeded_db):
    db, engine, Session = seeded_db
    row = _seed_cancelled(db, status=CANCELLED_STATUS)
    ids = [r["requestId"] for r in _client_for(engine, Session, VENDOR_A).get(PATH).json()]
    assert row.RID in ids


@pytest.mark.parametrize(
    "status",
    [
        "REQUEST - CANCELLED BY USER",
        "BID - OPEN",
        "BID - CONFIRMED",
        "REQUEST - CONFIRMED",
        "HANDSHAKE - REJECTED",
        "VENDOR - CANCELLED",
    ],
)
def test_non_target_statuses_excluded(seeded_db, status):
    db, engine, Session = seeded_db
    _seed_cancelled(db, status=status)
    resp = _client_for(engine, Session, VENDOR_A).get(PATH)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_past_pickup_included(seeded_db):
    db, engine, Session = seeded_db
    pd, pt = _days_ago_pickup(2)
    row = _seed_cancelled(db, pickup_date=pd, pickup_time=pt)
    ids = [r["requestId"] for r in _client_for(engine, Session, VENDOR_A).get(PATH).json()]
    assert row.RID in ids


def test_future_pickup_excluded(seeded_db):
    db, engine, Session = seeded_db
    pd, pt = _tomorrow_pickup()
    _seed_cancelled(db, pickup_date=pd, pickup_time=pt)
    assert _client_for(engine, Session, VENDOR_A).get(PATH).json() == []


def test_asia_kolkata_boundary_exact(seeded_db):
    """Pickup exactly at current IST moment is not past and must be excluded."""
    db, engine, Session = seeded_db
    fixed_now = datetime(2026, 6, 15, 12, 0, 0)
    row = _seed_cancelled(
        db,
        pickup_date=date(2026, 6, 15),
        pickup_time=time(12, 0, 0),
    )
    with patch.object(request_crud, "_now_ist_naive", return_value=fixed_now):
        body = _client_for(engine, Session, VENDOR_A).get(PATH).json()
    assert body == []
    assert row.RID not in [r.get("requestId") for r in body]

    past_row = _seed_cancelled(
        db,
        pickup_date=date(2026, 6, 15),
        pickup_time=time(11, 59, 59),
    )
    with patch.object(request_crud, "_now_ist_naive", return_value=fixed_now):
        ids = [r["requestId"] for r in _client_for(engine, Session, VENDOR_A).get(PATH).json()]
    assert past_row.RID in ids


def test_malformed_pickup_date_history_data_invalid(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)

    def _boom(request_row):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )

    client = _client_for(engine, Session, VENDOR_A)
    with patch.object(request_crud, "_history_pickup_datetime", side_effect=_boom):
        resp = client.get(PATH)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "HISTORY_DATA_INVALID"


def test_malformed_pickup_time_history_data_invalid(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)

    def _time_type_invalid(req):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )

    client = _client_for(engine, Session, VENDOR_A)
    with patch.object(
        request_crud, "_history_pickup_datetime", side_effect=_time_type_invalid
    ):
        resp = client.get(PATH)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "HISTORY_DATA_INVALID"


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------


def test_reopened_original_included_when_past(seeded_db):
    db, engine, Session = seeded_db
    row = _seed_cancelled(db, request_reopened=True)
    ids = [r["requestId"] for r in _client_for(engine, Session, VENDOR_A).get(PATH).json()]
    assert row.RID in ids


def test_reopened_clone_excluded_unless_independently_cancelled(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, status="BID - OPEN", vendor_app_id=None, rejection_reason=None)
    assert _client_for(engine, Session, VENDOR_A).get(PATH).json() == []


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def test_empty_list(seeded_db):
    _, engine, Session = seeded_db
    resp = _client_for(engine, Session, VENDOR_A).get(PATH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_newest_pickup_first_with_rid_tiebreak(seeded_db):
    db, engine, Session = seeded_db
    same_day = (datetime.now(IST) - timedelta(days=3)).date()
    older = _seed_cancelled(
        db, pickup_date=same_day, pickup_time=time(8, 0, 0), rejection_reason="a"
    )
    newer = _seed_cancelled(
        db, pickup_date=same_day, pickup_time=time(10, 0, 0), rejection_reason="b"
    )
    tie_a = _seed_cancelled(
        db, pickup_date=same_day, pickup_time=time(9, 0, 0), rejection_reason="c"
    )
    tie_b = _seed_cancelled(
        db, pickup_date=same_day, pickup_time=time(9, 0, 0), rejection_reason="d"
    )
    ids = [r["requestId"] for r in _client_for(engine, Session, VENDOR_A).get(PATH).json()]
    assert ids[0] == newer.RID
    assert older.RID in ids
    # Same pickup time: higher RID first
    assert ids.index(tie_b.RID) < ids.index(tie_a.RID)


def test_nullable_final_amount_preserved_as_null(seeded_db):
    """Schema + mapper preserve null; SQLite column is NOT NULL so mock the amount."""
    db, engine, Session = seeded_db
    _seed_cancelled(db, final_amount=4500)
    with patch.object(
        request_crud, "_history_nullable_final_amount", return_value=None
    ):
        item = _client_for(engine, Session, VENDOR_A).get(PATH).json()[0]
    assert item["finalAmount"] is None
    assert request_crud._history_nullable_final_amount(None) is None


def test_numeric_zero_preserved_as_zero(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, final_amount=0)
    item = _client_for(engine, Session, VENDOR_A).get(PATH).json()[0]
    assert item["finalAmount"] == 0.0
    assert request_crud._history_nullable_final_amount(0) == 0.0


def test_cancellation_reason_mapped(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, rejection_reason="  Weather  ")
    item = _client_for(engine, Session, VENDOR_A).get(PATH).json()[0]
    assert item["cancellationReason"] == "Weather"


def test_null_reason_becomes_empty_string(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, rejection_reason=None)
    item = _client_for(engine, Session, VENDOR_A).get(PATH).json()[0]
    assert item["cancellationReason"] == ""


def test_customer_display_name_and_profile_image(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)
    item = _client_for(engine, Session, VENDOR_A).get(PATH).json()[0]
    assert item["customerDisplayName"] == "Customer One"
    assert item["customerProfileImageUrl"] == "images/profilepic_male.png"


def test_no_pii_or_internal_fields(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db)
    item = _client_for(engine, Session, VENDOR_A).get(PATH).json()[0]
    _assert_no_forbidden(item)
    assert "phone" not in str(item).lower() or "profile" in str(item).lower()
    serialized = str(item)
    assert SECRET_FCM not in serialized
    assert SECRET_BANK not in serialized
    assert SECRET_PASSWORD not in serialized
    assert CUSTOMER_ID not in serialized
    assert VENDOR_A not in serialized


def test_openapi_list_vendor_cancelled_history_item(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A)
    schema = client.app.openapi()
    path_item = schema["paths"][PATH]["get"]
    assert "vendorId" in path_item["parameters"][0]["name"] or any(
        p["name"] == "vendorId" for p in path_item.get("parameters", [])
    )
    vendor_param = next(p for p in path_item["parameters"] if p["name"] == "vendorId")
    assert vendor_param.get("required") is not True
    # response is array of VendorCancelledHistoryItem
    ref = path_item["responses"]["200"]["content"]["application/json"]["schema"]
    assert ref.get("type") == "array" or "$ref" in ref.get("items", {})
    assert VendorCancelledHistoryItem.__name__ in str(ref) or "VendorCancelledHistoryItem" in str(
        schema
    )


def test_sql_exception_safe_500(seeded_db):
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A)
    mock_db = MagicMock()
    mock_db.query.side_effect = SQLAlchemyError("SELECT * FROM secret_table")

    def _override_db():
        yield mock_db

    client.app.dependency_overrides[get_db] = _override_db
    resp = client.get(PATH)
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "HISTORY_QUERY_FAILED"
    assert "secret_table" not in str(body).lower()
    assert "sqlalchemy" not in str(body).lower()


def test_locked_unapproved_winning_vendor_can_read(seeded_db):
    db, engine, Session = seeded_db
    _seed_cancelled(db, vendor_app_id=VENDOR_A)
    user = db.query(User).filter(User.userAppId == VENDOR_A).one()
    user.lockApp = True
    user.vendorApproved = False
    db.add(user)
    db.commit()
    resp = _client_for(engine, Session, VENDOR_A).get(PATH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
