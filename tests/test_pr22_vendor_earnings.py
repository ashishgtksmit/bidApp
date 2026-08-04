"""
PR22 vendor earnings — GET /vendor/earnings FastAPI endpoint.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
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
from app_v1.models.customer_reviews import CustomerReview  # noqa: E402
from app_v1.endpoints.reporting import router as reporting_router  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.crud import vendor_earnings as earnings_crud  # noqa: E402
from app_v1.schemas.vendor_earnings import VendorEarningsReport  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
PATH = "/vendor/earnings"
CONFIRMED = "REQUEST - CONFIRMED"

CUSTOMER_ID = "7022359323"
OTHER_CUSTOMER = "7000000003"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
LOSING_BIDDER = "8637554399"

SECRET_FCM = "secret-fcm-token-should-not-leak"
SECRET_BANK = "SECRET-BANK-ACCOUNT-1234"
SECRET_PASSWORD = "secret-password-should-not-leak"
SECRET_PHONE = "9000000000"

PR22_CORE_TABLES = [
    User.__table__,
    Request.__table__,
    CustomerReview.__table__,
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
    "fcmToken",
    "email",
    "emailId",
    "bankAccountNo",
    "paymentStatus",
    "PAYMENTSTATUS",
    "rejectionReason",
    "REJECTIONREASON",
    "driverName",
    "DRIVERNAME",
    "customerDisplayName",
    "customerProfileImageUrl",
    "driverProfileImageUrl",
    "driverNumber",
    "driverLicense",
    "registrationDoc",
    "carRegistrationNumber",
    "carModel",
    "customerReviewDone",
    "customerGeneralRating",
    "customerReviewComments",
}


# ---------------------------------------------------------------------------
# Engine / schema bootstrap
# ---------------------------------------------------------------------------


def _create_extra_sqlite(engine) -> None:
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
    Base.metadata.create_all(bind=engine, tables=PR22_CORE_TABLES)
    _create_extra_sqlite(engine)


def _memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    return engine


_bid_id_seq = {"n": 0}



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
    cr_counter = {"n": 0}
    _bid_id_seq["n"] = 0

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            req_counter["n"] += 1
            target.RID = req_counter["n"]

    def _assign_cr(mapper, connection, target):
        if getattr(target, "CR", None) is None:
            cr_counter["n"] += 1
            target.CR = cr_counter["n"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(CustomerReview, "before_insert", _assign_cr)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(CustomerReview, "before_insert", _assign_cr)


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
    _add_user(db, user_app_id=OTHER_CUSTOMER, uid=3, full_name="Customer Two")
    _add_user(db, user_app_id=VENDOR_B, uid=4, full_name="Vendor B")
    _add_user(db, user_app_id=LOSING_BIDDER, uid=5, full_name="Losing Bidder")
    return db, engine, Session


def _client_for(engine, Session, user_id, *, include_request_router: bool = False):
    app = FastAPI()
    app.include_router(reporting_router)
    if include_request_router:
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

def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    vendor_app_id: str | None = VENDOR_A,
    status: str = CONFIRMED,
    pickup_date=None,
    pickup_time=None,
    final_amount: int = 4500,
    from_location: str = "Gangtok",
    to_location: str = "Siliguri",
    payment_status: str | None = "PAID",
    rejection_reason: str | None = None,
    request_reopened: bool = False,
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
        noOfAdults=2,
        noOfKids=0,
        carType="Sedan",
        acRequest=True,
        carrierRequest=False,
        specialRequest="",
        requestStatus=status,
        paymentStatus=payment_status,
        customerAppId=customer_app_id,
        requestWonBy=vendor_app_id,
        finalAmount=final_amount,
        noOfBids=1,
        rejectionReason=rejection_reason,
        requestReopened=request_reopened,
        reviewDone="N",
        customerReviewDone="N",
        driverAssignedID=None,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_bid(db, *, rid: int, bidder_id: str, amount: float = 4500.0) -> None:
    _bid_id_seq["n"] += 1
    bid_id = _bid_id_seq["n"]
    db.execute(
        text(
            """
            INSERT INTO biddetails
                (BID, rID, bidderID, CARID, bidAmount, bidStatus, tableTimestamp, last_updated)
            VALUES
                (:bid, :rid, :bidder, NULL, :amount, :status, :ts, :ts)
            """
        ),
        {
            "bid": bid_id,
            "rid": rid,
            "bidder": int(bidder_id),
            "amount": amount,
            "status": CONFIRMED,
            "ts": "2026-01-01 12:00:00",
        },
    )
    db.commit()


def _seed_customer_review(
    db,
    *,
    rid,
    giver_app_id: str,
    receiver_app_id: str,
    general_rating="4.5",
    comments: str = "Great trip",
) -> CustomerReview:
    row = CustomerReview(
        RID=str(rid),
        ratingGiverUserAppId=giver_app_id,
        ratingReceiverUserAppId=receiver_app_id,
        generalRating=Decimal(general_rating),
        comments=comments,
        tableTimestamp=datetime(2026, 1, 1, 10, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _set_final_amount_sql(db, rid: int, value) -> None:
    db.execute(
        text("UPDATE requesttable SET finalAmount = :val WHERE RID = :rid"),
        {"val": value, "rid": rid},
    )
    db.commit()


def _earnings(client: TestClient, **params):
    return client.get(PATH, params=params or None)


def _assert_no_forbidden(body) -> None:
    if isinstance(body, dict):
        for key in body:
            assert key not in FORBIDDEN_KEYS, f"forbidden key in response: {key}"
            _assert_no_forbidden(body[key])
    elif isinstance(body, list):
        for item in body:
            _assert_no_forbidden(item)


def _months_back_start(end: date, months: int) -> date:
    """Inclusive calendar-month span of `months` ending in end's month."""
    y, m = end.year, end.month
    for _ in range(months - 1):
        if m == 1:
            y -= 1
            m = 12
        else:
            m -= 1
    return date(y, m, 1)


# ---------------------------------------------------------------------------
# Authentication (1-2)
# ---------------------------------------------------------------------------


def test_earnings_without_jwt_unauthenticated(seeded_db):
    """(1) No JWT → 401/403 when get_current_user_id override is absent."""
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, user_id=None)
    resp = _earnings(client)
    assert resp.status_code in (401, 403)


def test_earnings_invalid_jwt_unauthenticated(seeded_db):
    """(2) Invalid JWT → 401 when real auth dependency is used (no override)."""
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, user_id=None)
    resp = client.get(
        PATH,
        headers={"Authorization": "Bearer not-a-valid-jwt-token"},
    )
    assert resp.status_code == 401
    assert "credentials" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Identity (3-9)
# ---------------------------------------------------------------------------


def test_no_vendor_id_query_param_required(seeded_db):
    """(3) Success without vendorId query param."""
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    resp = _earnings(_client_for(engine, Session, VENDOR_A))
    assert resp.status_code == 200
    assert resp.json()["summary"]["completedTripCount"] == 1


def test_winning_vendor_sees_own_report(seeded_db):
    """(4) Winning vendor sees own past confirmed trip."""
    db, engine, Session = seeded_db
    req = _seed_request(db, vendor_app_id=VENDOR_A, final_amount=5200)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["summary"]["grossBookingValue"] == 5200
    assert body["trips"][0]["requestId"] == req.RID
    assert body["trips"][0]["grossAmount"] == 5200


def test_losing_bidder_row_excluded(seeded_db):
    """(5) Losing bidder must not see vendor A's confirmed trip."""
    db, engine, Session = seeded_db
    req = _seed_request(db, vendor_app_id=VENDOR_A)
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A)
    _seed_bid(db, rid=req.RID, bidder_id=LOSING_BIDDER, amount=4300.0)
    body = _earnings(_client_for(engine, Session, LOSING_BIDDER)).json()
    assert body["summary"]["completedTripCount"] == 0
    assert body["trips"] == []


def test_another_vendor_row_excluded(seeded_db):
    """(6) Vendor A does not see vendor B's trips."""
    db, engine, Session = seeded_db
    keep = _seed_request(db, vendor_app_id=VENDOR_A, from_location="Mine")
    _seed_request(db, vendor_app_id=VENDOR_B, from_location="Theirs")
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["trips"][0]["requestId"] == keep.RID


def test_null_request_won_by_excluded(seeded_db):
    """(7) Rows with null requestWonBy are excluded."""
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=None, status=CONFIRMED)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 0


def test_locked_vendor_can_read_own_report(seeded_db):
    """(8) lockApp=True vendor can still read earnings."""
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    user = db.query(User).filter(User.userAppId == VENDOR_A).one()
    user.lockApp = True
    db.add(user)
    db.commit()
    resp = _earnings(_client_for(engine, Session, VENDOR_A))
    assert resp.status_code == 200
    assert resp.json()["summary"]["completedTripCount"] == 1


def test_unapproved_vendor_can_read_own_report(seeded_db):
    """(9) vendorApproved=False vendor can still read earnings."""
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    user = db.query(User).filter(User.userAppId == VENDOR_A).one()
    user.vendorApproved = False
    db.add(user)
    db.commit()
    resp = _earnings(_client_for(engine, Session, VENDOR_A))
    assert resp.status_code == 200
    assert resp.json()["summary"]["completedTripCount"] == 1


# ---------------------------------------------------------------------------
# Eligibility (10-18)
# ---------------------------------------------------------------------------


def test_past_request_confirmed_included(seeded_db):
    """(10) Past REQUEST - CONFIRMED included."""
    db, engine, Session = seeded_db
    pd, pt = _days_ago_pickup(3)
    req = _seed_request(db, status=CONFIRMED, pickup_date=pd, pickup_time=pt)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert req.RID in {t["requestId"] for t in body["trips"]}


def test_future_request_confirmed_excluded(seeded_db):
    """(11) Future REQUEST - CONFIRMED excluded."""
    db, engine, Session = seeded_db
    fd, ft = _tomorrow_pickup()
    _seed_request(db, status=CONFIRMED, pickup_date=fd, pickup_time=ft)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 0


@pytest.mark.parametrize(
    "status",
    [
        "BOOKING - CANCELLED BY USER",  # (12)
        "REQUEST - CANCELLED BY USER",  # (13)
        "BID - OPEN",  # (14)
        "BID - CONFIRMED",  # (15)
    ],
)
def test_ineligible_statuses_excluded(seeded_db, status):
    db, engine, Session = seeded_db
    _seed_request(db, status=status)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 0


def test_reopened_cancelled_original_excluded(seeded_db):
    """(16) Cancelled original with requestReopened=True excluded."""
    db, engine, Session = seeded_db
    pd, pt = _yesterday_pickup()
    _seed_request(
        db,
        status="BOOKING - CANCELLED BY USER",
        pickup_date=pd,
        pickup_time=pt,
        request_reopened=True,
    )
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 0


def test_reopened_clone_open_excluded(seeded_db):
    """(17) Reopened clone still BID - OPEN excluded."""
    db, engine, Session = seeded_db
    pd, pt = _yesterday_pickup()
    _seed_request(db, status="BID - OPEN", pickup_date=pd, pickup_time=pt, vendor_app_id=None)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 0


def test_reopened_clone_included_after_confirmed_past(seeded_db):
    """(18) Reopened clone included once independently confirmed and past."""
    db, engine, Session = seeded_db
    pd, pt = _yesterday_pickup()
    original = _seed_request(
        db,
        status="BOOKING - CANCELLED BY USER",
        pickup_date=pd,
        pickup_time=pt,
        request_reopened=True,
        from_location="Original",
    )
    clone = _seed_request(
        db,
        status=CONFIRMED,
        pickup_date=pd,
        pickup_time=pt,
        from_location="Clone",
    )
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    ids = {t["requestId"] for t in body["trips"]}
    assert original.RID not in ids
    assert clone.RID in ids


# ---------------------------------------------------------------------------
# Amount (19-26)
# ---------------------------------------------------------------------------


def test_positive_final_amount_included(seeded_db):
    """(19) Positive finalAmount included in summary and trips."""
    db, engine, Session = seeded_db
    _seed_request(db, final_amount=7500)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["grossBookingValue"] == 7500
    assert body["trips"][0]["grossAmount"] == 7500


def test_zero_final_amount_in_trip_count(seeded_db):
    """(20) Zero finalAmount still counts as a completed trip."""
    db, engine, Session = seeded_db
    _seed_request(db, final_amount=0, from_location="ZeroTrip")
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert len(body["trips"]) == 1


def test_zero_final_amount_contributes_zero_to_total(seeded_db):
    """(21) Zero finalAmount adds 0 to grossBookingValue."""
    db, engine, Session = seeded_db
    _seed_request(db, final_amount=0)
    _seed_request(db, final_amount=1000, from_location="Paid")
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 2
    assert body["summary"]["grossBookingValue"] == 1000


def test_null_final_amount_treated_as_zero(seeded_db):
    """(22) NULL finalAmount treated as 0.

    Request.finalAmount is NOT NULL in schema; helper still coerces None → 0.
    End-to-end path uses explicit zero amount.
    """
    from types import SimpleNamespace

    assert (
        earnings_crud._non_negative_amount(
            SimpleNamespace(RID=99, finalAmount=None)
        )
        == 0
    )

    db, engine, Session = seeded_db
    _seed_request(db, final_amount=0)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["summary"]["grossBookingValue"] == 0
    assert body["trips"][0]["grossAmount"] == 0


def test_negative_final_amount_excluded(seeded_db):
    """(23) Negative finalAmount excluded entirely."""
    db, engine, Session = seeded_db
    req = _seed_request(db, final_amount=100)
    _set_final_amount_sql(db, req.RID, -500)
    _seed_request(db, final_amount=2000, from_location="Good")
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["summary"]["grossBookingValue"] == 2000


def test_bid_amount_not_used_for_gross(seeded_db):
    """(24) Report uses finalAmount, not bidAmount."""
    db, engine, Session = seeded_db
    req = _seed_request(db, final_amount=3000)
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=9999.0)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["grossBookingValue"] == 3000
    assert body["trips"][0]["grossAmount"] == 3000


def test_multiple_bid_rows_no_duplicate_inflation(seeded_db):
    """(25) Multiple bid rows must not inflate trip count or gross total."""
    db, engine, Session = seeded_db
    req = _seed_request(db, final_amount=4000)
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=4000.0)
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=4100.0)
    _seed_bid(db, rid=req.RID, bidder_id=LOSING_BIDDER, amount=3900.0)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["summary"]["grossBookingValue"] == 4000


def test_review_rows_no_duplicate_inflation(seeded_db):
    """(26) Customer review rows must not inflate trip count or gross total.

    Earnings query does not join reviews. Schema enforces one review per RID;
    presence of a review must leave trip count/gross unchanged.
    """
    db, engine, Session = seeded_db
    req = _seed_request(db, final_amount=2500)
    _seed_customer_review(
        db,
        rid=req.RID,
        giver_app_id=CUSTOMER_ID,
        receiver_app_id=VENDOR_A,
    )
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["summary"]["grossBookingValue"] == 2500


# ---------------------------------------------------------------------------
# No-range report (27-38)
# ---------------------------------------------------------------------------


def test_no_range_period_start_null_and_end_today_ist(seeded_db):
    """(27)(28) Default report: periodStart null, periodEnd is current IST date."""
    db, engine, Session = seeded_db
    _seed_request(db)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["periodStart"] is None
    assert body["periodEnd"] == datetime.now(IST).date().isoformat()


def test_no_range_summary_counts_all_eligible_past_rows(seeded_db):
    """(29) Summary includes every eligible past confirmed trip."""
    db, engine, Session = seeded_db
    for i in range(3):
        pd, pt = _days_ago_pickup(i + 1, hour=8 + i)
        _seed_request(db, pickup_date=pd, pickup_time=pt, final_amount=1000 * (i + 1))
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 3
    assert body["summary"]["grossBookingValue"] == 1000 + 2000 + 3000


def test_no_range_newest_ten_trips_only(seeded_db):
    """(30) Trips list capped at newest 10."""
    db, engine, Session = seeded_db
    seeded = []
    for i in range(12):
        pd, pt = _days_ago_pickup(i + 1, hour=10)
        seeded.append(_seed_request(db, pickup_date=pd, pickup_time=pt, from_location=f"T{i}"))
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["completedTripCount"] == 12
    assert len(body["trips"]) == 10


def test_no_range_trip_order_pickup_desc_with_rid_tiebreak(seeded_db):
    """(31)(32) Trips sorted by pickup desc; same pickup → higher RID first."""
    db, engine, Session = seeded_db
    older_d, older_t = _days_ago_pickup(5, hour=8)
    newer_d, newer_t = _days_ago_pickup(1, hour=10)
    same_d, same_t = _days_ago_pickup(2, hour=12)
    r_old = _seed_request(db, pickup_date=older_d, pickup_time=older_t, from_location="Old")
    r_same_a = _seed_request(db, pickup_date=same_d, pickup_time=same_t, from_location="SameA")
    r_same_b = _seed_request(db, pickup_date=same_d, pickup_time=same_t, from_location="SameB")
    r_new = _seed_request(db, pickup_date=newer_d, pickup_time=newer_t, from_location="New")
    ids = [t["requestId"] for t in _earnings(_client_for(engine, Session, VENDOR_A)).json()["trips"]]
    assert ids[0] == r_new.RID
    assert ids.index(r_same_b.RID) < ids.index(r_same_a.RID)
    assert ids[-1] == r_old.RID


def test_no_range_exactly_six_monthly_buckets(seeded_db):
    """(33) Default report returns exactly six monthly buckets."""
    _, engine, Session = seeded_db
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert len(body["monthlyBuckets"]) == 6


def test_no_range_current_month_bucket_present(seeded_db):
    """(34) Current IST month appears in monthlyBuckets."""
    _, engine, Session = seeded_db
    today = datetime.now(IST).date()
    month_abbrev = today.strftime("%b")
    expected = f"{month_abbrev} {today.year}"
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    labels = [b["label"] for b in body["monthlyBuckets"]]
    assert expected in labels
    assert labels[-1] == expected


def test_no_range_zero_months_included(seeded_db):
    """(35) Months with no trips still appear with zero totals."""
    _, engine, Session = seeded_db
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert len(body["monthlyBuckets"]) == 6
    assert all(b["completedTripCount"] == 0 for b in body["monthlyBuckets"])
    assert all(b["grossBookingValue"] == 0 for b in body["monthlyBuckets"])


def test_no_range_cross_year_bucket_labels(seeded_db):
    """(36) Bucket labels use MMM yyyy across year boundary."""
    db, engine, Session = seeded_db
    fixed_today = date(2026, 1, 15)
    with patch.object(earnings_crud, "_today_ist", return_value=fixed_today):
        body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    labels = [b["label"] for b in body["monthlyBuckets"]]
    assert labels[0] == "Aug 2025"
    assert labels[-1] == "Jan 2026"
    assert len(labels) == 6


def test_no_range_bucket_totals_and_counts(seeded_db):
    """(37)(38) Monthly bucket trip counts and gross totals match seeded pickups."""
    db, engine, Session = seeded_db
    fixed_today = date(2026, 3, 10)
    with patch.object(earnings_crud, "_today_ist", return_value=fixed_today):
        with patch.object(
            earnings_crud,
            "_now_ist_naive",
            return_value=datetime(2026, 3, 10, 23, 59, 59),
        ):
            jan_trip = _seed_request(
                db,
                pickup_date=date(2026, 1, 5),
                pickup_time=time(9, 0, 0),
                final_amount=1000,
                from_location="Jan",
            )
            feb_a = _seed_request(
                db,
                pickup_date=date(2026, 2, 1),
                pickup_time=time(10, 0, 0),
                final_amount=2000,
                from_location="FebA",
            )
            _seed_request(
                db,
                pickup_date=date(2026, 2, 15),
                pickup_time=time(11, 0, 0),
                final_amount=500,
                from_location="FebB",
            )
            body = _earnings(_client_for(engine, Session, VENDOR_A)).json()

    buckets = {b["label"]: b for b in body["monthlyBuckets"]}
    assert buckets["Jan 2026"]["completedTripCount"] == 1
    assert buckets["Jan 2026"]["grossBookingValue"] == 1000
    assert buckets["Feb 2026"]["completedTripCount"] == 2
    assert buckets["Feb 2026"]["grossBookingValue"] == 2500
    assert buckets["Mar 2026"]["completedTripCount"] == 0
    assert buckets["Mar 2026"]["grossBookingValue"] == 0
    assert jan_trip.RID
    assert feb_a.RID


# ---------------------------------------------------------------------------
# Range report (39-53)
# ---------------------------------------------------------------------------


def test_range_both_dates_omitted_succeeds(seeded_db):
    """(39) Omitting startDate and endDate succeeds (default report)."""
    db, engine, Session = seeded_db
    _seed_request(db)
    resp = _earnings(_client_for(engine, Session, VENDOR_A))
    assert resp.status_code == 200


def test_range_only_start_date_422(seeded_db):
    """(40) Only startDate → 422."""
    _, engine, Session = seeded_db
    resp = _earnings(
        _client_for(engine, Session, VENDOR_A),
        startDate="2026-01-01",
    )
    assert resp.status_code == 422


def test_range_only_end_date_422(seeded_db):
    """(41) Only endDate → 422."""
    _, engine, Session = seeded_db
    resp = _earnings(
        _client_for(engine, Session, VENDOR_A),
        endDate="2026-01-31",
    )
    assert resp.status_code == 422


def test_range_invalid_date_format_422(seeded_db):
    """(42) Invalid date format → 422."""
    _, engine, Session = seeded_db
    resp = _earnings(
        _client_for(engine, Session, VENDOR_A),
        startDate="01-01-2026",
        endDate="2026-01-31",
    )
    assert resp.status_code == 422


def test_range_start_after_end_422(seeded_db):
    """(43) startDate > endDate → 422."""
    _, engine, Session = seeded_db
    resp = _earnings(
        _client_for(engine, Session, VENDOR_A),
        startDate="2026-02-01",
        endDate="2026-01-01",
    )
    assert resp.status_code == 422


def test_range_24_month_span_accepted(seeded_db):
    """(44) Exactly 24 calendar months is accepted."""
    db, engine, Session = seeded_db
    end = datetime.now(IST).date()
    start = _months_back_start(end, 24)
    _seed_request(
        db,
        pickup_date=start,
        pickup_time=time(9, 0, 0),
        from_location="RangeStart",
    )
    resp = _earnings(
        _client_for(engine, Session, VENDOR_A),
        startDate=start.isoformat(),
        endDate=end.isoformat(),
    )
    assert resp.status_code == 200
    assert resp.json()["summary"]["completedTripCount"] >= 1


def test_range_over_24_months_422(seeded_db):
    """(45) Span > 24 months → 422 REPORT_RANGE_TOO_LARGE."""
    end = datetime.now(IST).date()
    start = _months_back_start(end, 25)
    _, engine, Session = seeded_db
    resp = _earnings(
        _client_for(engine, Session, VENDOR_A),
        startDate=start.isoformat(),
        endDate=end.isoformat(),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "REPORT_RANGE_TOO_LARGE"


def test_range_inclusive_start_date(seeded_db):
    """(46) Pickup on startDate is included."""
    db, engine, Session = seeded_db
    start = date(2026, 2, 1)
    end = date(2026, 2, 28)
    req = _seed_request(
        db,
        pickup_date=start,
        pickup_time=time(8, 0, 0),
        from_location="StartBoundary",
    )
    with patch.object(
        earnings_crud,
        "_now_ist_naive",
        return_value=datetime(2026, 3, 1, 12, 0, 0),
    ):
        body = _earnings(
            _client_for(engine, Session, VENDOR_A),
            startDate=start.isoformat(),
            endDate=end.isoformat(),
        ).json()
    assert req.RID in {t["requestId"] for t in body["trips"]}


def test_range_inclusive_end_date(seeded_db):
    """(47) Pickup on endDate is included."""
    db, engine, Session = seeded_db
    start = date(2026, 2, 1)
    end = date(2026, 2, 28)
    req = _seed_request(
        db,
        pickup_date=end,
        pickup_time=time(18, 0, 0),
        from_location="EndBoundary",
    )
    with patch.object(
        earnings_crud,
        "_now_ist_naive",
        return_value=datetime(2026, 3, 1, 12, 0, 0),
    ):
        body = _earnings(
            _client_for(engine, Session, VENDOR_A),
            startDate=start.isoformat(),
            endDate=end.isoformat(),
        ).json()
    assert req.RID in {t["requestId"] for t in body["trips"]}


def test_range_future_end_allowed_but_future_pickup_excluded(seeded_db):
    """(48) Future endDate allowed; future pickup still excluded from summary."""
    db, engine, Session = seeded_db
    past_d, past_t = _yesterday_pickup()
    future_d, future_t = _tomorrow_pickup()
    keep = _seed_request(db, pickup_date=past_d, pickup_time=past_t, from_location="Past")
    _seed_request(
        db,
        pickup_date=future_d,
        pickup_time=future_t,
        from_location="Future",
    )
    future_end = (datetime.now(IST) + timedelta(days=30)).date().isoformat()
    body = _earnings(
        _client_for(engine, Session, VENDOR_A),
        startDate=(datetime.now(IST) - timedelta(days=60)).date().isoformat(),
        endDate=future_end,
    ).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["trips"][0]["requestId"] == keep.RID


def test_range_summary_limited_to_range(seeded_db):
    """(49) Summary counts only pickups inside [startDate, endDate]."""
    db, engine, Session = seeded_db
    in_range = _seed_request(
        db,
        pickup_date=date(2026, 1, 10),
        pickup_time=time(9, 0, 0),
        final_amount=1500,
        from_location="In",
    )
    _seed_request(
        db,
        pickup_date=date(2025, 12, 20),
        pickup_time=time(9, 0, 0),
        final_amount=9000,
        from_location="Before",
    )
    with patch.object(
        earnings_crud,
        "_now_ist_naive",
        return_value=datetime(2026, 2, 1, 12, 0, 0),
    ):
        body = _earnings(
            _client_for(engine, Session, VENDOR_A),
            startDate="2026-01-01",
            endDate="2026-01-31",
        ).json()
    assert body["summary"]["completedTripCount"] == 1
    assert body["summary"]["grossBookingValue"] == 1500
    assert body["trips"][0]["requestId"] == in_range.RID


def test_range_trips_limited_to_range(seeded_db):
    """(50) Trips list contains only in-range pickups."""
    db, engine, Session = seeded_db
    in_trip = _seed_request(
        db,
        pickup_date=date(2026, 1, 5),
        pickup_time=time(9, 0, 0),
        from_location="InRange",
    )
    out_trip = _seed_request(
        db,
        pickup_date=date(2025, 11, 1),
        pickup_time=time(9, 0, 0),
        from_location="OutRange",
    )
    with patch.object(
        earnings_crud,
        "_now_ist_naive",
        return_value=datetime(2026, 2, 1, 12, 0, 0),
    ):
        ids = {
            t["requestId"]
            for t in _earnings(
                _client_for(engine, Session, VENDOR_A),
                startDate="2026-01-01",
                endDate="2026-01-31",
            ).json()["trips"]
        }
    assert in_trip.RID in ids
    assert out_trip.RID not in ids


def test_range_monthly_buckets_cover_intersecting_months(seeded_db):
    """(51)(52) Range buckets include every intersecting month, even with zero trips."""
    _, engine, Session = seeded_db
    with patch.object(
        earnings_crud,
        "_now_ist_naive",
        return_value=datetime(2026, 4, 1, 12, 0, 0),
    ):
        body = _earnings(
            _client_for(engine, Session, VENDOR_A),
            startDate="2026-01-01",
            endDate="2026-03-31",
        ).json()
    labels = [b["label"] for b in body["monthlyBuckets"]]
    assert labels == ["Jan 2026", "Feb 2026", "Mar 2026"]
    assert all(b["completedTripCount"] == 0 for b in body["monthlyBuckets"])


def test_range_newest_ten_trips_only(seeded_db):
    """(53) Range report still caps trips at newest 10."""
    db, engine, Session = seeded_db
    start = date(2026, 1, 1)
    end = date(2026, 3, 31)
    for i in range(11):
        d = date(2026, 1, 1) + timedelta(days=i)
        _seed_request(
            db,
            pickup_date=d,
            pickup_time=time(9, 0, 0),
            from_location=f"R{i}",
        )
    with patch.object(
        earnings_crud,
        "_now_ist_naive",
        return_value=datetime(2026, 4, 1, 12, 0, 0),
    ):
        body = _earnings(
            _client_for(engine, Session, VENDOR_A),
            startDate=start.isoformat(),
            endDate=end.isoformat(),
        ).json()
    assert body["summary"]["completedTripCount"] == 11
    assert len(body["trips"]) == 10


# ---------------------------------------------------------------------------
# Response (54-67)
# ---------------------------------------------------------------------------


def test_response_currency_inr(seeded_db):
    """(54) Summary currency is INR."""
    db, engine, Session = seeded_db
    _seed_request(db)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    assert body["summary"]["currency"] == "INR"


def test_empty_report_returns_200_zero_report(seeded_db):
    """(55) No eligible trips → 200 with zero-valued report."""
    _, engine, Session = seeded_db
    resp = _earnings(_client_for(engine, Session, VENDOR_A))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["completedTripCount"] == 0
    assert body["summary"]["grossBookingValue"] == 0
    assert body["summary"]["currency"] == "INR"
    assert body["trips"] == []
    assert len(body["monthlyBuckets"]) == 6


def test_response_excludes_forbidden_pii_and_internal_fields(seeded_db):
    """(56-63) Response must not expose forbidden PII / internal fields."""
    db, engine, Session = seeded_db
    req = _seed_request(
        db,
        final_amount=4500,
        payment_status="PAID",
        rejection_reason="should-not-leak",
    )
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A)
    body = _earnings(_client_for(engine, Session, VENDOR_A)).json()
    _assert_no_forbidden(body)
    serialized = str(body)
    assert SECRET_FCM not in serialized
    assert SECRET_BANK not in serialized
    assert SECRET_PASSWORD not in serialized
    assert SECRET_PHONE not in serialized
    assert CUSTOMER_ID not in serialized
    assert VENDOR_A not in serialized
    assert "should-not-leak" not in serialized
    assert "paymentstatus" not in serialized.lower()


def test_openapi_vendor_earnings_report_schema(seeded_db):
    """(64) OpenAPI documents /vendor/earnings as VendorEarningsReport."""
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A)
    schema = client.app.openapi()
    path_item = schema["paths"][PATH]["get"]
    ref = path_item["responses"]["200"]["content"]["application/json"]["schema"]
    assert VendorEarningsReport.__name__ in str(ref) or "$ref" in ref
    assert "VendorEarningsReport" in str(schema)


def test_sql_exception_report_query_failed(seeded_db):
    """(65) SQLAlchemyError → 500 REPORT_QUERY_FAILED without SQL leakage."""
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A)
    mock_db = MagicMock()
    mock_db.query.side_effect = SQLAlchemyError("SELECT * FROM secret_table")

    def _override_db():
        yield mock_db

    client.app.dependency_overrides[get_db] = _override_db
    resp = _earnings(client)
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "REPORT_QUERY_FAILED"
    assert "secret_table" not in str(body).lower()
    assert "sqlalchemy" not in str(body).lower()
    assert "select" not in str(body).lower()


def test_no_sql_text_in_error_response(seeded_db):
    """(66) Error responses must not echo SQL fragments."""
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A)
    mock_db = MagicMock()
    mock_db.query.side_effect = SQLAlchemyError("UPDATE requesttable SET x=1")

    def _override_db():
        yield mock_db

    client.app.dependency_overrides[get_db] = _override_db
    resp = _earnings(client)
    assert resp.status_code == 500
    assert "update" not in str(resp.json()).lower()
    assert "requesttable" not in str(resp.json()).lower()


def test_crud_does_not_close_request_scoped_db_session(seeded_db):
    """(67) get_vendor_earnings_report leaves the request-scoped session usable."""
    db, engine, Session = seeded_db
    _seed_request(db, vendor_app_id=VENDOR_A)
    earnings_crud.get_vendor_earnings_report(db, user_id=VENDOR_A)
    assert db.query(Request).count() >= 1
    db.execute(text("SELECT 1"))


def test_getbookingreport_still_on_request_router(seeded_db):
    """Regression: legacy getbookingreport remains on request_router OpenAPI."""
    _, engine, Session = seeded_db
    client = _client_for(engine, Session, VENDOR_A, include_request_router=True)
    schema = client.app.openapi()
    assert "/getbookingreport" in schema["paths"]
