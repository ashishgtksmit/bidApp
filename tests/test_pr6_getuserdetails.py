"""
PR6 GET /getuserdetails session-profile contract tests.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.schemas.user_table import GetUserDetailsResponse, NoUserResponse  # noqa: E402
from app_v1.crud.user import get_user_details  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402

CUSTOMER_ID = "7022359323"
OTHER_USER = "7000000002"



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

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_customer(db, *, uid: int = 1, user_app_id: str = "7022359323"):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="8637554387",
        fullName="Customer User",
        emailId="customer@example.com",
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


def _add_vendor(db, *, uid: int = 2, user_app_id: str = "9876543210"):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="9000000001",
        fullName="Vendor User",
        emailId="vendor@example.com",
        dob="1988-05-15",
        city="Siliguri",
        gender="Female",
        profilePicture="https://example.com/v.png",
        alsoVendor=True,
        vendorApproved=True,
        lockApp=False,
        customerRating="4.8",
        totalCustomerReviews=3,
        rating="4.2",
        totalNoOfReviews=27,
        fcmToken="dummy-fcm",
        user_login_status="LOGGEDIN",
    )
    db.add(user)
    db.commit()
    return user


def test_getuserdetails_customer_response(db):
    _add_customer(db)
    result = get_user_details(db, userAppId="7022359323")

    assert isinstance(result, list)
    assert len(result) == 1
    row = result[0]
    assert isinstance(row, GetUserDetailsResponse)
    assert row.USERAPPID == "7022359323"
    assert row.FULLNAME == "Customer User"
    assert row.EMAILID == "customer@example.com"
    assert row.EMAIL == "customer@example.com"
    assert row.DOB == "1990-01-01"
    assert row.CITY == "Gangtok"
    assert row.GENDER == "Male"
    assert row.PROFILEPIC == "images/profilepic_male.png"
    assert row.ALSOVENDOR is False
    assert row.VENDOR is False
    assert row.CUSTOMERRATING == "4.5"
    assert row.TOTALCUSTOMERRATING == 12
    assert row.VENDORRATING is None
    assert row.TOTALVENDORRATING is None


def test_getuserdetails_vendor_response(db):
    _add_vendor(db)
    result = get_user_details(db, userAppId="9876543210")

    assert isinstance(result, list)
    row = result[0]
    assert row.USERAPPID == "9876543210"
    assert row.ALSOVENDOR is True
    assert row.VENDOR is True
    assert row.CUSTOMERRATING == "4.8"
    assert row.TOTALCUSTOMERRATING == 3
    assert row.VENDORRATING == pytest.approx(4.2)
    assert row.TOTALVENDORRATING == 27
    # Legacy fields remain vendor-column mirrors; clients must prefer CUSTOMERRATING.
    assert row.RATING == pytest.approx(4.2)
    assert row.TOTALREVIEWS == 27


def test_getuserdetails_non_vendor_vendor_ratings_null(db):
    _add_customer(db)
    row = get_user_details(db, userAppId="7022359323")[0]
    assert row.ALSOVENDOR is False
    assert row.VENDORRATING is None
    assert row.TOTALVENDORRATING is None


def test_getuserdetails_userappid_present(db):
    _add_customer(db, user_app_id="1111222233")
    row = get_user_details(db, userAppId="1111222233")[0]
    assert row.USERAPPID == "1111222233"


def test_getuserdetails_missing_user(db):
    result = get_user_details(db, userAppId="0000000000")
    assert isinstance(result, NoUserResponse)
    assert result.message == "NO REGISTERED"


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _endpoint_client(engine, Session, user_id: str):
    app = FastAPI()
    app.include_router(user_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app)


@pytest.fixture()
def endpoint_db():
    engine, Session = _memory_db()
    db = Session()
    try:
        yield db, engine, Session
    finally:
        db.close()


def test_getuserdetails_endpoint_matching_user_app_id_succeeds(endpoint_db):
    db, engine, Session = endpoint_db
    _add_customer(db, user_app_id=CUSTOMER_ID)
    client = _endpoint_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID})
    assert response.status_code == 200
    row = response.json()[0]
    assert row["USERAPPID"] == CUSTOMER_ID
    assert row["FULLNAME"] == "Customer User"


def test_getuserdetails_endpoint_queryless_succeeds(endpoint_db):
    """PR38 — current Flutter path: no userAppId query."""
    db, engine, Session = endpoint_db
    _add_customer(db, user_app_id=CUSTOMER_ID)
    client = _endpoint_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["USERAPPID"] == CUSTOMER_ID


def test_getuserdetails_endpoint_mismatched_user_app_id_403(endpoint_db):
    db, engine, Session = endpoint_db
    _add_customer(db, user_app_id=CUSTOMER_ID)
    client = _endpoint_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails", params={"userAppId": OTHER_USER})
    assert response.status_code == 403
