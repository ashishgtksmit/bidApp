"""
PR18 vendor trip preferences — JWT-owned GET/PUT preference endpoints.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import patch

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
from app_v1.auth.deps import get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.region_details import Region  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.models.request_type_details import RequestType  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402
from app_v1.endpoints.location import router as location_router  # noqa: E402
from app_v1.utils.common import _parse_id_list_strict, _to_id_array  # noqa: E402

CUSTOMER_ID = "7022359323"
PENDING_VENDOR = "8637554387"
APPROVED_VENDOR = "8637554388"
LOCKED_USER = "7000000001"
OTHER_VENDOR = "7000000002"
MISSING_USER = "7999999999"

PR18_TABLES = [
    User.__table__,
    Region.__table__,
    LocationDetail.__table__,
    RequestType.__table__,
]


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR18_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr18_client(engine, Session, user_id: str | None):
    app = FastAPI()
    app.include_router(user_router)
    app.include_router(location_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    if user_id is not None:
        app.dependency_overrides[get_current_user_id] = lambda: user_id
    return TestClient(app)


def _add_user(db, *, user_app_id: str, uid: int, **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="1000000000",
        fullName=kwargs.get("fullName", "User"),
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
        joiningDate=kwargs.get("joiningDate", date(2024, 1, 15)),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 12),
        user_login_status="LOGGEDOUT",
        cityPreferences=kwargs.get("cityPreferences", "10,11"),
        requestTypePreferences=kwargs.get("requestTypePreferences", "1,2"),
        regionPreferences=kwargs.get("regionPreferences", "1"),
        address=kwargs.get("address", "Line address"),
        state=kwargs.get("state", "Sikkim"),
        bankAccountHolderName=kwargs.get("bankAccountHolderName", "Holder"),
        bankAccountNo=kwargs.get("bankAccountNo", "123456789012"),
        bankIFSC=kwargs.get("bankIFSC", "SBIN0001234"),
        bankName=kwargs.get("bankName", "SBI"),
        imageAadhar=kwargs.get("imageAadhar", "https://example.com/aadhaar.png"),
        imagePAN=kwargs.get("imagePAN", "https://example.com/pan.png"),
        imageBankAccount=kwargs.get(
            "imageBankAccount", "https://example.com/passbook.png"
        ),
        tableTimestamp=kwargs.get("tableTimestamp", None),
    )
    db.add(user)
    db.commit()
    return user


def _seed_catalog(db):
    db.add(Region(RDID=1, regionName="East"))
    db.add(Region(RDID=2, regionName="West"))
    db.add(
        LocationDetail(
            LID=10, location="Gangtok", location_shortCode="GTK", regionId=1
        )
    )
    db.add(
        LocationDetail(
            LID=11, location="Pakyong", location_shortCode="PKY", regionId=1
        )
    )
    db.add(
        LocationDetail(
            LID=20, location="Pelling", location_shortCode="PEL", regionId=2
        )
    )
    db.add(RequestType(RTDID=1, requestType="One Way"))
    db.add(RequestType(RTDID=2, requestType="Round Trip"))
    db.add(RequestType(RTDID=5, requestType="Airport"))
    db.commit()


@pytest.fixture
def db_session():
    engine, Session = _memory_db()
    db = Session()
    try:
        _seed_catalog(db)
        yield db, engine, Session
    finally:
        db.close()


# --- Parser -----------------------------------------------------------------


def test_to_id_array_accepts_ints_and_digit_strings():
    assert _to_id_array([3, "1", 2, 2, "1"]) == [1, 2, 3]


def test_parse_id_list_strict_rejects_bool_float_zero_negative():
    with pytest.raises(ValueError):
        _parse_id_list_strict([True], field_name="CITYIDS")
    with pytest.raises(ValueError):
        _parse_id_list_strict([1.5], field_name="CITYIDS")
    with pytest.raises(ValueError):
        _parse_id_list_strict([0], field_name="CITYIDS")
    with pytest.raises(ValueError):
        _parse_id_list_strict([-1], field_name="CITYIDS")
    with pytest.raises(ValueError):
        _parse_id_list_strict(["abc"], field_name="CITYIDS")
    assert _parse_id_list_strict([3, 1, 1, "2"], field_name="CITYIDS") == [1, 2, 3]
    assert _parse_id_list_strict([], field_name="CITYIDS") == []


# --- Auth -------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/getuserregionpreferences"),
        ("get", "/getuserrequesttypepreferences"),
        ("put", "/updateregioncityselections"),
        ("put", "/updaterequesttypeselections"),
    ],
)
def test_routes_without_jwt_return_401(db_session, method, path):
    _, engine, Session = db_session
    app = FastAPI()
    app.include_router(user_router)
    app.include_router(location_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    if method == "get":
        response = client.get(path)
    else:
        body = (
            {"regionIds": [], "cityIds": []}
            if "region" in path
            else {"requestTypeIds": []}
        )
        response = client.put(path, json=body)
    assert response.status_code in (401, 403)


# --- Eligibility / ownership -----------------------------------------------


def test_missing_user_get_region_404(db_session):
    _, engine, Session = db_session
    client = _pr18_client(engine, Session, MISSING_USER)
    response = client.get("/getuserregionpreferences")
    assert response.status_code == 404
    assert response.json()["detail"] == "USER_NOT_FOUND"


def test_customer_not_eligible(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        alsoVendor=False,
        vendorApproved=False,
    )
    client = _pr18_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserregionpreferences")
    assert response.status_code == 403
    assert response.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_pending_vendor_not_eligible(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=2,
        alsoVendor=True,
        vendorApproved=False,
    )
    client = _pr18_client(engine, Session, PENDING_VENDOR)
    response = client.get("/getuserrequesttypepreferences")
    assert response.status_code == 403
    assert response.json()["detail"] == "VENDOR_NOT_ELIGIBLE"


def test_locked_vendor_account_locked_before_eligibility(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=LOCKED_USER,
        uid=3,
        alsoVendor=False,
        vendorApproved=False,
        lockApp=True,
    )
    client = _pr18_client(engine, Session, LOCKED_USER)
    response = client.put(
        "/updaterequesttypeselections",
        json={"requestTypeIds": [1]},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "ACCOUNT_LOCKED"


def test_mismatched_user_app_id_forbidden(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=4)
    _add_user(db, user_app_id=OTHER_VENDOR, uid=5)
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.get(
        "/getuserregionpreferences",
        params={"userAppId": OTHER_VENDOR},
    )
    assert response.status_code == 403


def test_user_a_cannot_update_user_b(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=6,
        cityPreferences="10",
        regionPreferences="1",
    )
    _add_user(
        db,
        user_app_id=OTHER_VENDOR,
        uid=7,
        cityPreferences="20",
        regionPreferences="2",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ):
        response = client.put(
            "/updateregioncityselections",
            json={
                "userAppId": OTHER_VENDOR,
                "regionIds": [1],
                "cityIds": [10, 11],
            },
        )
    assert response.status_code == 403
    db.expire_all()
    other = db.query(User).filter(User.userAppId == OTHER_VENDOR).one()
    assert other.cityPreferences == "20"


# --- GET region/city --------------------------------------------------------


def test_get_region_preferences_catalog_and_selected(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=8,
        regionPreferences="1",
        cityPreferences="10",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getuserregionpreferences")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    east = next(r for r in payload if r["REGION_ID"] == 1)
    assert east["SELECTED"] is True
    cities = {c["LID"]: c for c in east["CITIES"]}
    assert cities[10]["SELECTED"] is True
    assert cities[11]["SELECTED"] is False
    assert cities[10]["CITY"] == "Gangtok"
    blob = str(payload)
    assert "regionPreferences" not in blob
    assert "userAppId" not in blob
    assert "alsoVendor" not in blob


def test_get_region_empty_prefs_all_false(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=9,
        regionPreferences="",
        cityPreferences=None,
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getuserregionpreferences")
    assert response.status_code == 200
    payload = response.json()
    assert all(r["SELECTED"] is False for r in payload)
    assert all(
        c["SELECTED"] is False for r in payload for c in r["CITIES"]
    )


def test_get_region_malformed_csv_safe(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=10,
        regionPreferences="1,abc,,2",
        cityPreferences="10,xx,11",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getuserregionpreferences")
    assert response.status_code == 200
    payload = response.json()
    east = next(r for r in payload if r["REGION_ID"] == 1)
    assert east["SELECTED"] is True


# --- GET request types ------------------------------------------------------


def test_get_request_type_preferences(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=11,
        requestTypePreferences="1,5",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getuserrequesttypepreferences")
    assert response.status_code == 200
    payload = response.json()
    by_id = {row["REQUEST_TYPE_ID"]: row for row in payload}
    assert by_id[1]["SELECTED"] is True
    assert by_id[2]["SELECTED"] is False
    assert by_id[5]["SELECTED"] is True
    assert "requestTypePreferences" not in str(payload)


def test_get_request_type_empty_all_false(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=12,
        requestTypePreferences="",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.get("/getuserrequesttypepreferences")
    assert response.status_code == 200
    assert all(row["SELECTED"] is False for row in response.json())


# --- PUT region/city --------------------------------------------------------


def test_put_region_city_requires_both_arrays(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=13)
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updateregioncityselections",
        json={"regionIds": [1]},
    )
    assert response.status_code == 422


def test_put_region_city_success_dedupe_sort_and_propagate(db_session):
    db, engine, Session = db_session
    user = _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=14,
        regionPreferences="1",
        cityPreferences="10",
        bankAccountNo="123456789012",
        fcmToken="secret-fcm-token-should-not-leak",
    )
    original_bank = user.bankAccountNo
    original_fcm = user.fcmToken
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.put(
            "/updateregioncityselections",
            json={"regionIds": [2, 1, 1], "cityIds": [20, 10, 10]},
        )
    assert response.status_code == 200
    assert response.json() == {"message": "UPDATED"}
    refresh.assert_called_once_with(APPROVED_VENDOR)
    db.expire_all()
    updated = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert updated.regionPreferences == "1,2"
    assert updated.cityPreferences == "10,20"
    assert updated.bankAccountNo == original_bank
    assert updated.fcmToken == original_fcm
    assert updated.requestTypePreferences == "1,2"


def test_put_region_city_empty_allowed(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=15,
        regionPreferences="1",
        cityPreferences="10",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.put(
            "/updateregioncityselections",
            json={"regionIds": [], "cityIds": []},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    refresh.assert_called_once()
    db.expire_all()
    updated = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert updated.regionPreferences == ""
    assert updated.cityPreferences == ""


def test_put_region_city_same_value_updated_no_publish(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=16,
        regionPreferences="1,2",
        cityPreferences="10,20",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.put(
            "/updateregioncityselections",
            json={"regionIds": [2, 1], "cityIds": [20, 10]},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    refresh.assert_not_called()


def test_put_region_city_unknown_ids_422(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=17,
        regionPreferences="1",
        cityPreferences="10",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updateregioncityselections",
        json={"regionIds": [99], "cityIds": [10]},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "ERROR_INVALID_REGIONIDS"
    response = client.put(
        "/updateregioncityselections",
        json={"regionIds": [1], "cityIds": [999]},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "ERROR_INVALID_CITYIDS"
    db.expire_all()
    unchanged = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert unchanged.regionPreferences == "1"
    assert unchanged.cityPreferences == "10"


def test_put_region_city_rejects_bool_float(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=18)
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updateregioncityselections",
        json={"regionIds": [True], "cityIds": [10]},
    )
    assert response.status_code == 422
    response = client.put(
        "/updateregioncityselections",
        json={"regionIds": [1], "cityIds": [1.5]},
    )
    assert response.status_code == 422


def test_put_region_city_lock_before_mutation(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=19, cityPreferences="10")
    client = _pr18_client(engine, Session, APPROVED_VENDOR)

    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    user.lockApp = True
    db.commit()
    response = client.put(
        "/updateregioncityselections",
        json={"regionIds": [1], "cityIds": [11]},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "ACCOUNT_LOCKED"


# --- PUT request types ------------------------------------------------------


def test_put_request_types_success_and_dynamic_ids(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=20,
        requestTypePreferences="1",
        cityPreferences="10",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.put(
            "/updaterequesttypeselections",
            json={"requestTypeIds": [5, 1, 1]},
        )
    assert response.status_code == 200
    assert response.json() == {"message": "UPDATED"}
    refresh.assert_called_once_with(APPROVED_VENDOR)
    db.expire_all()
    updated = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert updated.requestTypePreferences == "1,5"
    assert updated.cityPreferences == "10"
    assert "NOTHING_TO_UDPATE" not in str(response.json())


def test_put_request_types_empty_allowed(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=21,
        requestTypePreferences="1,2",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ):
        response = client.put(
            "/updaterequesttypeselections",
            json={"requestTypeIds": []},
        )
    assert response.status_code == 200
    db.expire_all()
    updated = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert updated.requestTypePreferences == ""


def test_put_request_types_unknown_422(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=22,
        requestTypePreferences="1",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    response = client.put(
        "/updaterequesttypeselections",
        json={"requestTypeIds": [99]},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "ERROR_INVALID_REQUESTTYPEIDS"
    db.expire_all()
    unchanged = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert unchanged.requestTypePreferences == "1"


def test_put_request_types_same_value_no_publish(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=23,
        requestTypePreferences="1,5",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ) as refresh:
        response = client.put(
            "/updaterequesttypeselections",
            json={"requestTypeIds": [5, 1]},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    refresh.assert_not_called()


def test_propagation_failure_still_returns_updated(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=24,
        requestTypePreferences="1",
    )
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=False
    ):
        response = client.put(
            "/updaterequesttypeselections",
            json={"requestTypeIds": [2]},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    db.expire_all()
    updated = db.query(User).filter(User.userAppId == APPROVED_VENDOR).one()
    assert updated.requestTypePreferences == "2"


def test_no_user_app_id_required_on_get_put(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=APPROVED_VENDOR, uid=25)
    client = _pr18_client(engine, Session, APPROVED_VENDOR)
    assert client.get("/getuserregionpreferences").status_code == 200
    assert client.get("/getuserrequesttypepreferences").status_code == 200
    with patch(
        "app_v1.crud.user.request_vendor_snapshot_refresh", return_value=True
    ):
        assert (
            client.put(
                "/updateregioncityselections",
                json={"regionIds": [1], "cityIds": [10]},
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/updaterequesttypeselections",
                json={"requestTypeIds": [1]},
            ).status_code
            == 200
        )
