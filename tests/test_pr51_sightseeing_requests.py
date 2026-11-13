"""
PR51/PR53 sightseeing: spot catalog, itinerary on insert/update/reopen,
history completion, vendor targeting and FCM routing keys.

In-memory SQLite; does not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
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

from app_v1.auth.deps import AuthenticatedUser, get_current_user  # noqa: E402
from app_v1.crud import sightseeing as ss  # noqa: E402
from app_v1.crud.request import (  # noqa: E402
    _history_completion_datetime,
    create_request,
    reopen_request,
    update_request,
)
from app_v1.database import Base, get_db  # noqa: E402
from app_v1.endpoints.location import router as location_router  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.request_type_details import RequestType  # noqa: E402
from app_v1.models.sightseeing import (  # noqa: E402
    RequestItineraryDay,
    RequestItineraryDaySpot,
    SightseeingSpot,
)
from app_v1.models.user_table import User  # noqa: E402
from app_v1.schemas.request_table import RequestCreate, RequestUpdate  # noqa: E402
from app_v1.services.vendor_filtering import get_vendors_for_request  # noqa: E402
from app_v1.utils.fcm import _build_fcm_message  # noqa: E402

CUSTOMER_ID = "7022359323"
SS_TYPE_ID = 7
GANGTOK = 11
DARJEELING = 12

TABLES = [
    User.__table__,
    Request.__table__,
    RequestType.__table__,
    LocationDetail.__table__,
    SightseeingSpot.__table__,
    RequestItineraryDay.__table__,
    RequestItineraryDaySpot.__table__,
]


@pytest.fixture(autouse=True)
def _sqlite_ids_and_cache():
    """SQLite does not autoincrement BigInteger PKs; reset the type cache."""
    counters = {}
    listeners = []
    for model, attr in (
        (Request, "RID"),
        (RequestItineraryDay, "dayId"),
        (RequestItineraryDaySpot, "id"),
    ):
        def _assign(mapper, connection, target, _attr=attr, _model=model):
            if getattr(target, _attr, None) is None:
                counters[_model] = counters.get(_model, 0) + 1
                setattr(target, _attr, counters[_model])

        event.listen(model, "before_insert", _assign)
        listeners.append((model, _assign))
    ss.reset_sightseeing_type_cache()
    try:
        yield
    finally:
        for model, fn in listeners:
            event.remove(model, "before_insert", fn)
        ss.reset_sightseeing_type_cache()


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine, tables=TABLES)
    return engine


def _seed(session, *, launched: bool = True):
    session.add(
        User(
            UID=1,
            userAppId=CUSTOMER_ID,
            password="secret",
            alternateNumber="8637554387",
            fullName="Customer User",
            emailId="c@example.com",
            dob="1990-01-01",
            city="Gangtok",
            gender="Male",
            alsoVendor=False,
            vendorApproved=False,
            lockApp=False,
            customerRating="4.5",
            totalCustomerReviews=0,
            rating="5",
            totalNoOfReviews=0,
            user_login_status="LOGGEDOUT",
        )
    )
    session.add(RequestType(RTDID=1, requestType="One Way"))
    if launched:
        session.add(RequestType(RTDID=SS_TYPE_ID, requestType="Sightseeing"))
    session.add(LocationDetail(LID=GANGTOK, location="Gangtok", regionId=1))
    session.add(LocationDetail(LID=DARJEELING, location="Darjeeling", regionId=1))
    session.add_all(
        [
            SightseeingSpot(spotId=1, locationId=GANGTOK, spotCode="TSOMGO_LAKE", spotName="Tsomgo Lake", permitRequired=True, isActive=True, sortOrder=10),
            SightseeingSpot(spotId=2, locationId=GANGTOK, spotCode="BABA_MANDIR", spotName="Baba Mandir", permitRequired=True, isActive=True, sortOrder=20),
            SightseeingSpot(spotId=3, locationId=GANGTOK, spotCode="MG_MARG", spotName="MG Marg", permitRequired=False, isActive=True, sortOrder=30),
            SightseeingSpot(spotId=4, locationId=GANGTOK, spotCode="OLD", spotName="Closed Spot", permitRequired=False, isActive=False, sortOrder=40),
            SightseeingSpot(spotId=5, locationId=DARJEELING, spotCode="TIGER_HILL", spotName="Tiger Hill", permitRequired=False, isActive=True, sortOrder=10),
        ]
    )
    session.commit()


@pytest.fixture()
def engine():
    eng = _engine()
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def seeded(session_factory):
    s = session_factory()
    _seed(s)
    s.close()
    return session_factory


D1 = date.today() + timedelta(days=10)
D2 = D1 + timedelta(days=1)


def _itinerary(**overrides):
    days = [
        {"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [1, 2]},
        {"dayNo": 2, "date": D2.isoformat(), "startTime": "08:00", "pickupLandmark": "Hotel X", "spotIds": [3]},
    ]
    days = overrides.pop("days", days)
    return days


def _create(**overrides) -> RequestCreate:
    body = {
        "fromLocation": "Gangtok",
        "fromLandmark": "Hotel X",
        "toLocation": "Gangtok",
        "toLandmark": "Hotel X",
        "pickUpDate": D1.isoformat(),
        "pickUpTime": "07:00",
        "noOfAdults": 2,
        "noOfKids": 0,
        "carType": "SUV",
        "acRequest": True,
        "carrierRequest": False,
        "bidEndTime": (datetime.combine(D1, time(0, 0)) - timedelta(days=1)).isoformat(sep=" "),
        "customerAppId": CUSTOMER_ID,
        "requestType": SS_TYPE_ID,
        "itinerary": _itinerary(),
    }
    body.update(overrides)
    return RequestCreate(**body)


def _insert(factory, data):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        return create_request(factory(), data, BackgroundTasks(), user_id=CUSTOMER_ID, notify=False)


# ---------------------------------------------------------------- catalog


def test_spots_for_location_active_and_ordered(seeded):
    s = seeded()
    spots = ss.list_spots_for_location(s, GANGTOK)
    s.close()
    assert [x.SPOTCODE for x in spots] == ["TSOMGO_LAKE", "BABA_MANDIR", "MG_MARG"]
    assert spots[0].PERMITREQUIRED is True
    assert spots[0].LOCATIONCODE == GANGTOK


def test_http_getsightseeingspots(seeded):
    app = FastAPI()
    app.include_router(location_router)

    def _db():
        s = seeded()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        uid=1,
        auth_subject="s",
        user_app_id=CUSTOMER_ID,
        account_session_id="a",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )
    client = TestClient(app)
    r = client.get("/getsightseeingspots", params={"locationCode": DARJEELING})
    assert r.status_code == 200
    assert [x["SPOTNAME"] for x in r.json()] == ["Tiger Hill"]
    assert client.get("/getsightseeingspots").status_code == 422


# ---------------------------------------------------------------- insert


def test_insert_sightseeing_persists_itinerary(seeded):
    assert _insert(seeded, _create()).message == "INSERTED"
    s = seeded()
    req = s.query(Request).one()
    assert req.requestType == SS_TYPE_ID
    assert req.tripEndDate == D2
    days = s.query(RequestItineraryDay).order_by(RequestItineraryDay.dayNo).all()
    assert [(d.dayNo, d.travelDate, d.pickupLandmark) for d in days] == [
        (1, D1, "Hotel X"),
        (2, D2, "Hotel X"),
    ]
    loaded = ss.load_itineraries(s, [req.RID])[req.RID]
    assert [sp["NAME"] for sp in loaded[0]["SPOTS"]] == ["Tsomgo Lake", "Baba Mandir"]
    assert loaded[0]["SPOTS"][0]["PERMIT"] is True
    assert loaded[1]["STARTTIME"] == "08:00:00"
    s.close()


def test_transfer_insert_unchanged(seeded):
    data = _create(requestType=None, itinerary=None, toLocation="Siliguri")
    assert _insert(seeded, data).message == "INSERTED"
    s = seeded()
    req = s.query(Request).one()
    assert req.requestType == 1
    assert req.tripEndDate is None
    assert s.query(RequestItineraryDay).count() == 0
    s.close()


def test_itinerary_rejected_before_launch(session_factory):
    s = session_factory()
    _seed(s, launched=False)
    s.close()
    result = _insert(session_factory, _create())
    assert result.message == "SIGHTSEEING_DISABLED"


def test_itinerary_rejected_on_transfer(seeded):
    result = _insert(seeded, _create(requestType=1))
    assert result.message == "INVALID_ITINERARY"


def test_sightseeing_requires_itinerary(seeded):
    result = _insert(seeded, _create(itinerary=None))
    assert result.message == "INVALID_ITINERARY"


@pytest.mark.parametrize(
    "days",
    [
        # Day 1 does not match pickUpDate
        [{"dayNo": 1, "date": D2.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [1]}],
        # Day 1 landmark mismatch
        [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Other", "spotIds": [1]}],
        # dates not ascending
        [
            {"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [1]},
            {"dayNo": 2, "date": D1.isoformat(), "startTime": "08:00", "pickupLandmark": "Hotel X", "spotIds": [3]},
        ],
        # gap in day numbers
        [
            {"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [1]},
            {"dayNo": 3, "date": D2.isoformat(), "startTime": "08:00", "pickupLandmark": "Hotel X", "spotIds": [3]},
        ],
        # spot from another city
        [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [5]}],
        # inactive spot
        [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [4]}],
        # repeated spot
        [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [1, 1]}],
        # unknown spot
        [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [999]}],
    ],
)
def test_invalid_itineraries(seeded, days):
    result = _insert(seeded, _create(itinerary=days))
    assert result.message == "INVALID_ITINERARY"
    s = seeded()
    assert s.query(Request).count() == 0
    assert s.query(RequestItineraryDay).count() == 0
    s.close()


def test_max_days_enforced(seeded, monkeypatch):
    monkeypatch.setenv("SIGHTSEEING_MAX_DAYS", "1")
    result = _insert(seeded, _create())
    assert result.message == "INVALID_ITINERARY"


def test_default_max_days_is_15(monkeypatch):
    monkeypatch.delenv("SIGHTSEEING_MAX_DAYS", raising=False)
    assert ss.sightseeing_max_days() == 15


def test_past_day1_rejected(seeded):
    past = date.today() - timedelta(days=1)
    days = [{"dayNo": 1, "date": past.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [1]}]
    result = _insert(seeded, _create(itinerary=days, pickUpDate=past.isoformat()))
    assert result.message == "INVALID_ITINERARY"


def test_duplicate_check_separates_transfer_and_sightseeing(seeded):
    assert _insert(seeded, _create()).message == "INSERTED"
    transfer_same_fields = _create(requestType=None, itinerary=None)
    assert _insert(seeded, transfer_same_fields).message == "INSERTED"
    assert _insert(seeded, _create()).message == "REQUEST_ALREADY_PRESENT"


# ---------------------------------------------------------------- update


def test_update_replaces_itinerary(seeded):
    assert _insert(seeded, _create()).message == "INSERTED"
    s = seeded()
    rid = s.query(Request.RID).scalar()
    s.close()
    new_days = [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": [3]}]
    body = RequestUpdate(
        RID=rid,
        fromLocation="Gangtok",
        fromLandmark="Hotel X",
        toLocation="Gangtok",
        toLandmark="Hotel X",
        pickUpDate=D1,
        pickUpTime=time(7, 0),
        noOfAdults=2,
        noOfKids=0,
        carType="SUV",
        acRequest=True,
        carrierRequest=False,
        itinerary=new_days,
    )
    assert update_request(seeded(), body, user_id=CUSTOMER_ID).message == "SUCCESS"
    s = seeded()
    assert s.query(Request).one().tripEndDate == D1
    loaded = ss.load_itineraries(s, [rid])[rid]
    assert len(loaded) == 1 and [x["NAME"] for x in loaded[0]["SPOTS"]] == ["MG Marg"]
    s.close()


def test_update_sightseeing_without_itinerary_rejected(seeded):
    assert _insert(seeded, _create()).message == "INSERTED"
    s = seeded()
    rid = s.query(Request.RID).scalar()
    s.close()
    body = RequestUpdate(RID=rid, fromLocation="Gangtok", fromLandmark="Hotel X", pickUpDate=D1, pickUpTime=time(7, 0))
    assert update_request(seeded(), body, user_id=CUSTOMER_ID).message == "INVALID_ITINERARY"


# ---------------------------------------------------------------- reopen


def test_reopen_clones_itinerary(seeded):
    assert _insert(seeded, _create()).message == "INSERTED"
    s = seeded()
    original = s.query(Request).one()
    original.requestStatus = "BOOKING - CANCELLED BY USER"
    original.bidEndTime = datetime.now() + timedelta(days=2)
    s.commit()
    rid = original.RID
    s.close()

    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]):
        result = reopen_request(seeded(), rid, BackgroundTasks(), user_id=CUSTOMER_ID)
    assert result.message == "UPDATED"
    s = seeded()
    new_row = s.query(Request).filter(Request.RID == result.newRequestId).one()
    assert new_row.requestType == SS_TYPE_ID
    assert new_row.tripEndDate == D2
    loaded = ss.load_itineraries(s, [rid, new_row.RID])
    assert loaded[new_row.RID] == loaded[rid]
    s.close()


# ---------------------------------------------------------------- history


def test_history_completion_uses_trip_end_date():
    req = Request(pickUpDate=D1, pickUpTime=time(7, 0), tripEndDate=D2)
    pickup = datetime.combine(D1, time(7, 0))
    assert _history_completion_datetime(req, pickup) == datetime.combine(D2 + timedelta(days=1), time.min)
    transfer = Request(pickUpDate=D1, pickUpTime=time(7, 0))
    assert _history_completion_datetime(transfer, pickup) == pickup


# ---------------------------------------------------------------- push


def test_vendor_targeting_filters_by_request_type(session_factory):
    s = session_factory()
    for uid, app_id, prefs in ((10, "v1", "1,2,3,4,7"), (11, "v2", "1,2,3,4")):
        s.add(
            User(
                UID=uid,
                userAppId=app_id,
                password="x",
                fullName=app_id,
                emailId=f"{app_id}@example.com",
                dob="1990-01-01",
                gender="Male",
                alternateNumber="0",
                alsoVendor=True,
                vendorApproved=True,
                lockApp=False,
                user_login_status="LOGGEDIN",
                fcmToken="tok",
                cityPreferences="",
                city="Gangtok",
                requestTypePreferences=prefs,
            )
        )
    s.commit()
    assert sorted(get_vendors_for_request(s, "Gangtok", "Gangtok")) == ["v1", "v2"]
    assert get_vendors_for_request(s, "Gangtok", "Gangtok", request_type=SS_TYPE_ID) == ["v1"]
    s.close()


def test_fcm_payload_routing_keys_only_when_set():
    plain = _build_fcm_message(title="t", body="b", url="u", target_token="x")["message"]["data"]
    assert "rid" not in plain and "requestType" not in plain
    routed = _build_fcm_message(
        title="t", body="b", url="u", target_token="x", request_id=42, request_type=SS_TYPE_ID
    )["message"]
    assert routed["data"]["rid"] == "42"
    assert routed["data"]["requestType"] == str(SS_TYPE_ID)
    assert routed["apns"]["payload"]["aps"]["custom_data"]["requestType"] == str(SS_TYPE_ID)
