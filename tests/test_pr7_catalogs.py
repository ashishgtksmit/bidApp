"""
PR7 catalog endpoint contract tests.

Covers GET /getlocations, /getregions, /getcities, /cartypedetails, /getrequesttypes
CRUD success shapes plus FastAPI auth rejection via a minimal app (no MySQL).
"""

from __future__ import annotations

import os
import sys
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

# crud.request → notifications → fcm requires firebase_admin (not needed here).
import types

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import get_current_user_id  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.models.region_details import Region  # noqa: E402
from app_v1.models.city_list import City  # noqa: E402
from app_v1.models.car_type_details import CarTypeDetail  # noqa: E402
from app_v1.models.request_type_details import RequestType  # noqa: E402
from app_v1.crud.location import (  # noqa: E402
    get_all_locations,
    get_all_cities,
    get_all_regions,
)
from app_v1.crud.car import get_all_car_types  # noqa: E402
from app_v1.crud.request import get_request_type  # noqa: E402
from app_v1.endpoints.location import router as location_router  # noqa: E402
from app_v1.endpoints.car import router as car_router  # noqa: E402
from app_v1.endpoints.request import router as request_router  # noqa: E402
from app_v1.schemas.location_details import LocationResponse  # noqa: E402
from app_v1.schemas.region_details import RegionDetailBase  # noqa: E402
from app_v1.schemas.city_list import CityListDetail  # noqa: E402
from app_v1.schemas.car_type_details import CarTypeDetailResponse  # noqa: E402
from app_v1.schemas.request_type_details import RequestTypeBase  # noqa: E402


CATALOG_TABLES = [
    LocationDetail.__table__,
    Region.__table__,
    City.__table__,
    CarTypeDetail.__table__,
    RequestType.__table__,
]


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=CATALOG_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded_db(db):
    db.add(Region(RDID=1, regionName="North Bengal"))
    db.add(
        LocationDetail(
            LID=61,
            location="Alipurduar",
            location_shortCode="ALP",
            regionId=1,
        )
    )
    db.add(
        LocationDetail(
            LID=62,
            location="Bagdogra",
            location_shortCode=None,
            regionId=1,
        )
    )
    db.add(City(CLID=10, cities="Gangtok", state="Sikkim"))
    db.add(City(CLID=11, cities="Siliguri", state="West Bengal"))
    db.add(
        CarTypeDetail(
            CTD=1,
            car_type="Sedan",
            car_sub_type="Dzire",
            capacity="4",
            image_url="https://example.com/sedan.png",
        )
    )
    db.add(
        CarTypeDetail(
            CTD=2,
            car_type="SUV",
            car_sub_type="Innova",
            capacity="6",
            image_url=None,
        )
    )
    db.add(RequestType(RTDID=5, requestType="One Way"))
    db.commit()
    return db


def test_getlocations_non_empty_exact_keys(seeded_db):
    result = get_all_locations(seeded_db)
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(row, LocationResponse) for row in result)
    # Ordered by LOCATION asc
    assert [row.LOCATION for row in result] == ["Alipurduar", "Bagdogra"]
    first = result[0].model_dump()
    assert set(first.keys()) == {
        "LOCATIONCODE",
        "LOCATION",
        "LOCATIONSHORTCODE",
        "REGIONID",
        "REGIONNAME",
    }
    assert first["LOCATIONCODE"] == 61
    assert first["LOCATION"] == "Alipurduar"
    assert first["LOCATIONSHORTCODE"] == "ALP"
    assert first["REGIONID"] == 1
    assert first["REGIONNAME"] == "North Bengal"
    # Nullable short code preserved
    assert result[1].LOCATIONSHORTCODE is None


def test_getlocations_empty_success(db):
    result = get_all_locations(db)
    assert result == []


def test_getregions_non_empty_exact_keys(seeded_db):
    result = get_all_regions(seeded_db)
    assert isinstance(result, list)
    assert len(result) == 1
    row = result[0]
    assert isinstance(row, RegionDetailBase)
    assert row.model_dump() == {"regionId": 1, "regionName": "North Bengal"}


def test_getregions_empty_success(db):
    assert get_all_regions(db) == []


def test_getcities_non_empty_ordered_exact_keys(seeded_db):
    result = get_all_cities(seeded_db)
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(row, CityListDetail) for row in result)
    assert [row.CITY for row in result] == ["Gangtok", "Siliguri"]
    first = result[0].model_dump()
    assert set(first.keys()) == {"CITYID", "CITY", "STATE"}
    assert first == {"CITYID": 10, "CITY": "Gangtok", "STATE": "Sikkim"}


def test_getcities_empty_success(db):
    assert get_all_cities(db) == []


def test_cartypedetails_non_empty_exact_keys_nullable_image(seeded_db):
    # get_all_car_types closes the session in finally; use a dedicated session.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=CATALOG_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        session.add(
            CarTypeDetail(
                CTD=1,
                car_type="Sedan",
                car_sub_type="Dzire",
                capacity="4",
                image_url="https://example.com/sedan.png",
            )
        )
        session.add(
            CarTypeDetail(
                CTD=2,
                car_type="SUV",
                car_sub_type="Innova",
                capacity="6",
                image_url=None,
            )
        )
        session.commit()
        result = get_all_car_types(session)
    finally:
        engine.dispose()

    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(row, CarTypeDetailResponse) for row in result)
    keys = set(result[0].model_dump().keys())
    assert keys == {"CTD", "CARTYPE", "CARSUBTYPE", "CAPACITY", "IMAGEURL"}
    assert result[0].CAPACITY == "4"
    assert result[0].IMAGEURL == "https://example.com/sedan.png"
    assert result[1].IMAGEURL is None


def test_cartypedetails_empty_success():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[CarTypeDetail.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        result = get_all_car_types(session)
    finally:
        engine.dispose()
    assert result == []


def test_getrequesttypes_non_empty_exact_keys():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[RequestType.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        session.add(RequestType(RTDID=5, requestType="One Way"))
        session.commit()
        result = get_request_type(session)
    finally:
        engine.dispose()

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], RequestTypeBase)
    assert result[0].model_dump() == {"RTDID": 5, "requestType": "One Way"}


def test_getrequesttypes_empty_success():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[RequestType.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        result = get_request_type(session)
    finally:
        engine.dispose()
    assert result == []


@pytest.fixture()
def catalog_engine():
    """Shared in-memory DB for HTTP tests (per-request sessions)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=CATALOG_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        session.add(Region(RDID=1, regionName="North Bengal"))
        session.add(
            LocationDetail(
                LID=61,
                location="Alipurduar",
                location_shortCode="ALP",
                regionId=1,
            )
        )
        session.add(City(CLID=10, cities="Gangtok", state="Sikkim"))
        session.add(
            CarTypeDetail(
                CTD=1,
                car_type="Sedan",
                car_sub_type="Dzire",
                capacity="4",
                image_url="https://example.com/sedan.png",
            )
        )
        session.add(RequestType(RTDID=5, requestType="One Way"))
        session.commit()
    finally:
        session.close()
    try:
        yield engine
    finally:
        engine.dispose()


def _catalog_client(engine):
    app = FastAPI()
    app.include_router(location_router)
    app.include_router(car_router)
    app.include_router(request_router)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        # Fresh session per request — some CRUD helpers call db.close().
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    return app


def test_catalog_endpoints_authenticated_success(catalog_engine):
    app = _catalog_client(catalog_engine)
    app.dependency_overrides[get_current_user_id] = lambda: "7022359323"
    client = TestClient(app)

    for path in (
        "/getlocations",
        "/getregions",
        "/getcities",
        "/cartypedetails",
        "/getrequesttypes",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert isinstance(body, list), path


def test_catalog_endpoints_reject_missing_jwt(catalog_engine):
    app = _catalog_client(catalog_engine)
    client = TestClient(app)

    for path in (
        "/getlocations",
        "/getregions",
        "/getcities",
        "/cartypedetails",
        "/getrequesttypes",
    ):
        response = client.get(path)
        assert response.status_code in (401, 403), path


def test_catalog_endpoints_reject_invalid_jwt(catalog_engine):
    app = _catalog_client(catalog_engine)
    client = TestClient(app)
    headers = {"Authorization": "Bearer not-a-valid-jwt"}

    for path in (
        "/getlocations",
        "/getregions",
        "/getcities",
        "/cartypedetails",
        "/getrequesttypes",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 401, path
