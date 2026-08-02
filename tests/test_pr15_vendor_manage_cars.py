"""
PR15 vendor Manage Cars — management list, catalog, create, soft-delete.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import base64
import os
import sys
import types
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
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
from app_v1.models.bid_details import BidDetail  # noqa: E402
from app_v1.models.car_details import CarDetail  # noqa: E402
from app_v1.models.car_type_details import CarTypeDetail  # noqa: E402
from app_v1.models.vendor_car_types import VendorCarType  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.crud import car_manage  # noqa: E402
from app_v1.crud import vendor_bid as vendor_bid_mod  # noqa: E402
from app_v1.schemas.car_details import (  # noqa: E402
    CreateVendorCarRequest,
    DeleteVendorCarRequest,
)
from app_v1.schemas.bid_details import VendorBidInsert  # noqa: E402
from app_v1.endpoints.car import router as car_router  # noqa: E402

CUSTOMER_ID = "7022359323"
VENDOR_A = "8637554387"
VENDOR_B = "8637554388"
NON_VENDOR = "7000000001"

# Minimal 1x1 PNG
_PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")

PR15_TABLES = [
    User.__table__,
    Request.__table__,
    CarDetail.__table__,
    CarTypeDetail.__table__,
    VendorCarType.__table__,
    LocationDetail.__table__,
]


def _create_biddetails_table(engine) -> None:
    # BidDetail.__table__ declares FKs to "requestTable"/"userTable" which do
    # not match the real table names ("requesttable"/"usertable"), so ORM
    # create_all() cannot resolve them under SQLite. Raw DDL mirrors PR11.
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
    Base.metadata.create_all(bind=engine, tables=PR15_TABLES)
    _create_biddetails_table(engine)


_bid_id_seq = {"n": 0}


@pytest.fixture(autouse=True)
def _sqlite_assign_ids():
    counters = {"rid": 0, "carid": 0, "vcrtid": 0}
    _bid_id_seq["n"] = 0

    def _assign_rid(mapper, connection, target):
        if getattr(target, "RID", None) is None:
            counters["rid"] += 1
            target.RID = counters["rid"]

    def _assign_carid(mapper, connection, target):
        if getattr(target, "CARID", None) is None:
            counters["carid"] += 1
            target.CARID = counters["carid"]

    def _assign_vcrtid(mapper, connection, target):
        if getattr(target, "VCRTID", None) is None:
            counters["vcrtid"] += 1
            target.VCRTID = counters["vcrtid"]

    event.listen(Request, "before_insert", _assign_rid)
    event.listen(CarDetail, "before_insert", _assign_carid)
    event.listen(VendorCarType, "before_insert", _assign_vcrtid)
    try:
        yield
    finally:
        event.remove(Request, "before_insert", _assign_rid)
        event.remove(CarDetail, "before_insert", _assign_carid)
        event.remove(VendorCarType, "before_insert", _assign_vcrtid)


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _prepare_engine(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr15_client(engine, Session, user_id: str):
    app = FastAPI()
    app.include_router(car_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    return TestClient(app)


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
    )
    db.add(user)
    db.commit()
    return user


def _seed_ctd(
    db,
    *,
    ctd: int = 1,
    car_type: str = "Sedan",
    car_sub_type: str = "Standard",
    capacity: str = "4",
    manufacturer: str = "Maruti",
    model: str = "Swift",
    variant: str = "VXI",
    year: str = "2020",
    fuel_type: str = "Petrol",
    seating_capacity: int = 4,
) -> None:
    """Seed a CarTypeDetail catalog row + a matching VendorCarType row.

    _validate_ctd_exists() in car_manage keys off VendorCarType.CTD, so both
    rows must exist for a CTD to pass "Add Car" validation.
    """
    db.add(
        CarTypeDetail(
            CTD=ctd,
            car_type=car_type,
            car_sub_type=car_sub_type,
            capacity=capacity,
            image_url=None,
        )
    )
    db.add(
        VendorCarType(
            manufacturer=manufacturer,
            model=model,
            variant=variant,
            year=year,
            fuelType=fuel_type,
            seatingCapacity=seating_capacity,
            CTD=ctd,
        )
    )
    db.commit()


def _seed_car(
    db,
    *,
    user_app_id: str,
    reg: str = "SK01A1111",
    model: str = "Swift",
    ctd: int = 1,
    admin_approved: bool = False,
    is_deleted: bool = False,
    owner_name: str = "Owner Name",
    car_owned_by_same_vendor: bool = True,
    registered_on: datetime | None = None,
    color: str = "White",
    model_year: str = "2020",
    front: str | None = "https://blob.example/front.jpg",
    side: str | None = "https://blob.example/side.jpg",
    poa: str | None = None,
    deleted_by: str | None = None,
    deleted_at: datetime | None = None,
) -> CarDetail:
    normalized = car_manage.normalize_car_registration(reg)
    row = CarDetail(
        userAppId=user_app_id,
        carRegNo=reg,
        normalizedCarRegNo=normalized,
        carColor=color,
        carModel=model,
        modelYear=model_year,
        ownerName=owner_name,
        registrationDoc="https://blob.example/rc.jpg",
        powerOfAttorneyDoc=poa,
        registeredOn=registered_on or datetime(2026, 1, 1, 12, 0, 0),
        adminApproved=admin_approved,
        carOwnedBySameVendor=car_owned_by_same_vendor,
        CTD=ctd,
        imageVehicleFront=front,
        imageVehicleSide=side,
        isDeleted=is_deleted,
        deletedAt=deleted_at,
        deletedBy=deleted_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_request(
    db,
    *,
    customer_app_id: str = CUSTOMER_ID,
    status: str = "BID - OPEN",
    request_won_by: str | None = None,
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
        specialRequest="Original",
        bidEndTime=datetime(2030, 8, 14, 18, 0, 0),
        requestStatus=status,
        customerAppId=customer_app_id,
        requestType=1,
        noOfBids=0,
        finalAmount=0,
        WIZZPNR="WIZZ123",
        paymentStatus=None,
        requestWonBy=request_won_by,
        tableTimestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_bid(
    db,
    *,
    rid: int,
    bidder_id: str,
    amount: float,
    status: str = "BID - OPEN",
    car_id: int | None = None,
) -> int:
    _bid_id_seq["n"] += 1
    bid_id = _bid_id_seq["n"]
    db.execute(
        text(
            """
            INSERT INTO biddetails
                (BID, rID, bidderID, CARID, bidAmount, bidStatus, tableTimestamp, last_updated)
            VALUES
                (:bid, :rid, :bidder, :car, :amount, :status, :ts, :ts)
            """
        ),
        {
            "bid": bid_id,
            "rid": rid,
            "bidder": int(bidder_id),
            "car": car_id,
            "amount": amount,
            "status": status,
            "ts": "2026-01-01 12:00:00",
        },
    )
    db.commit()
    return bid_id


def _create_body(**overrides) -> CreateVendorCarRequest:
    defaults = dict(
        carRegNo="SK01Z0000",
        carModel="Swift",
        CTD=1,
        carColor="White",
        modelYear=2022,
        ownerName="Vendor A",
        imageVehicleRC=_PNG_B64,
        imageVehicleFront=_PNG_B64,
        imageVehicleSide=_PNG_B64,
    )
    defaults.update(overrides)
    return CreateVendorCarRequest(**defaults)


def _mock_upload_ok(**kwargs):
    return True, f"https://blob.example/{kwargs['blob_name']}.jpg"


# ---------------------------------------------------------------------------
# Registration normalization (unit)
# ---------------------------------------------------------------------------


def test_normalize_car_registration_variants():
    assert car_manage.normalize_car_registration("SK 01 A 1111") == "SK01A1111"
    assert car_manage.normalize_car_registration("sk-01-a-1111") == "SK01A1111"
    assert car_manage.normalize_car_registration("Sk01A1111") == "SK01A1111"
    assert car_manage.normalize_car_registration("SK@01#A$1111!") == "SK01A1111"
    assert car_manage.normalize_car_registration("") == ""
    assert car_manage.normalize_car_registration("   ") == ""
    assert car_manage.normalize_car_registration(None) == ""


def test_create_car_empty_normalized_registration_rejected():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    db.close()

    db2 = Session()
    body = _create_body(carRegNo="---")
    with pytest.raises(HTTPException) as exc:
        car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert exc.value.status_code == 422
    assert exc.value.detail == "ERROR_INVALID_CARREGNO"


# ---------------------------------------------------------------------------
# Management list — GET /viewmanagedcarsforvendor
# ---------------------------------------------------------------------------


def test_management_list_pending_and_approved_newest_first_safe_fields():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    _seed_ctd(db, ctd=1)
    older = _seed_car(
        db,
        user_app_id=VENDOR_A,
        reg="SK01A0001",
        admin_approved=True,
        registered_on=datetime(2026, 1, 1, 10, 0, 0),
    )
    newer = _seed_car(
        db,
        user_app_id=VENDOR_A,
        reg="SK01A0002",
        admin_approved=False,
        registered_on=datetime(2026, 2, 1, 10, 0, 0),
    )
    _seed_car(db, user_app_id=VENDOR_B, reg="SK01A0003", admin_approved=True)
    older_id, newer_id = older.CARID, newer.CARID
    db.close()

    client = _pr15_client(engine, Session, VENDOR_A)
    resp = client.get("/viewmanagedcarsforvendor")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["CARID"] == newer_id
    assert data[1]["CARID"] == older_id
    assert data[0]["ADMINAPPROVED"] is False
    assert data[1]["ADMINAPPROVED"] is True
    for row in data:
        assert "USERAPPID" not in row
        assert "REGISTRATIONDOC" not in row
        assert "POWEROFATTORNEYDOC" not in row
        assert "ISDELETED" not in row
        assert "DELETEDAT" not in row
        assert "DELETEDBY" not in row


def test_management_list_soft_deleted_excluded():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    active = _seed_car(db, user_app_id=VENDOR_A, reg="SK01B0001", admin_approved=True)
    _seed_car(
        db,
        user_app_id=VENDOR_A,
        reg="SK01B0002",
        admin_approved=True,
        is_deleted=True,
        deleted_by=VENDOR_A,
        deleted_at=datetime(2026, 1, 2),
    )
    active_id = active.CARID
    db.close()

    client = _pr15_client(engine, Session, VENDOR_A)
    resp = client.get("/viewmanagedcarsforvendor")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["CARID"] == active_id


def test_management_list_empty_and_non_vendor_403():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(
        db,
        user_app_id=NON_VENDOR,
        uid=2,
        alsoVendor=False,
        vendorApproved=False,
    )
    db.close()

    client = _pr15_client(engine, Session, VENDOR_A)
    resp = client.get("/viewmanagedcarsforvendor")
    assert resp.status_code == 200
    assert resp.json() == []

    client_nv = _pr15_client(engine, Session, NON_VENDOR)
    resp2 = client_nv.get("/viewmanagedcarsforvendor")
    assert resp2.status_code == 403


def test_management_list_other_vendor_car_inaccessible():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    _seed_ctd(db, ctd=1)
    _seed_car(db, user_app_id=VENDOR_B, reg="SK01C0001", admin_approved=True)
    db.close()

    client = _pr15_client(engine, Session, VENDOR_A)
    resp = client.get("/viewmanagedcarsforvendor")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PR11 lean regression — GET /viewcarsforvendor
# ---------------------------------------------------------------------------


def test_pr11_lean_list_approved_active_included_pending_and_deleted_excluded():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1, car_type="Sedan")
    approved = _seed_car(db, user_app_id=VENDOR_A, reg="SK01D0001", admin_approved=True)
    _seed_car(db, user_app_id=VENDOR_A, reg="SK01D0002", admin_approved=False)
    _seed_car(
        db,
        user_app_id=VENDOR_A,
        reg="SK01D0003",
        admin_approved=True,
        is_deleted=True,
        deleted_by=VENDOR_A,
        deleted_at=datetime(2026, 1, 2),
    )
    approved_id = approved.CARID
    db.close()

    session = Session()
    result = vendor_bid_mod.get_vendor_cars_for_bidding(session, user_id=VENDOR_A)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].CARID == approved_id
    assert result[0].CAR_TYPE == "Sedan"
    assert set(result[0].model_dump().keys()) == {
        "CARID",
        "CARREGNO",
        "CARMODEL",
        "VEHICLE_FRONT",
        "CAR_TYPE",
    }


# ---------------------------------------------------------------------------
# Car types catalog — GET /getallvendorcartypes
# ---------------------------------------------------------------------------


def test_vendor_car_types_catalog_for_active_vendor_and_empty():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    db.close()

    session = Session()
    empty_result = car_manage.get_vendor_car_types_for_vendor(session, VENDOR_A)
    assert empty_result == []

    db2 = Session()
    _seed_ctd(
        db2,
        ctd=1,
        car_type="Sedan",
        car_sub_type="Standard",
        capacity="4",
        manufacturer="Maruti",
        model="Swift",
    )
    db2.close()

    session2 = Session()
    result = car_manage.get_vendor_car_types_for_vendor(session2, VENDOR_A)
    assert len(result) == 1
    row = result[0]
    assert row.manufacturer == "Maruti"
    assert row.model == "Swift"
    assert row.CTD == 1
    assert row.car_type == "Sedan"
    assert row.car_Sub_Type == "Standard"
    assert row.capacity == "4"


def test_vendor_car_types_non_vendor_403():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=NON_VENDOR,
        uid=1,
        alsoVendor=False,
        vendorApproved=False,
    )
    db.close()

    session = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.get_vendor_car_types_for_vendor(session, NON_VENDOR)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Create — POST /addcartoprofile
# ---------------------------------------------------------------------------


def test_create_car_jwt_owner_inserted_ignores_client_fields():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(
        carRegNo="SK01E0001",
        ownerName="Vendor A",
        userAppId="SHOULD_BE_IGNORED",
        adminApproved=True,
        CARID=999999,
    )
    db2 = Session()
    with patch.object(car_manage, "azure_blob_upload", side_effect=_mock_upload_ok), \
         patch.object(car_manage, "send_email", return_value={"message": "SENT"}):
        result = car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert result.message == "INSERTED"

    row = db2.query(CarDetail).filter(CarDetail.normalizedCarRegNo == "SK01E0001").one()
    assert row.userAppId == VENDOR_A
    assert row.CARID != 999999
    assert row.adminApproved is False
    assert row.isDeleted is False
    assert row.normalizedCarRegNo == "SK01E0001"


def test_create_car_duplicate_registration_conflicts():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _add_user(db, user_app_id=VENDOR_B, uid=2, full_name="Vendor B")
    _seed_ctd(db, ctd=1)
    db.close()

    with patch.object(car_manage, "azure_blob_upload", side_effect=_mock_upload_ok), \
         patch.object(car_manage, "send_email", return_value={"message": "SENT"}):
        db1 = Session()
        result = car_manage.insert_car_for_vendor(
            db1, _create_body(carRegNo="SK01F0001", ownerName="Vendor A"), VENDOR_A
        )
        assert result.message == "INSERTED"

        db2 = Session()
        with pytest.raises(HTTPException) as exc:
            car_manage.insert_car_for_vendor(
                db2,
                _create_body(carRegNo="sk-01-f-0001", ownerName="Vendor A"),
                VENDOR_A,
            )
        assert exc.value.status_code == 409
        assert exc.value.detail == "CAR_ALREADY_EXISTS"

        # Soft-delete then attempt to re-create — global uniqueness retained.
        db3 = Session()
        car_row = (
            db3.query(CarDetail)
            .filter(CarDetail.normalizedCarRegNo == "SK01F0001")
            .one()
        )
        car_row.isDeleted = True
        car_row.deletedAt = datetime(2026, 1, 2)
        car_row.deletedBy = VENDOR_A
        db3.commit()

        db4 = Session()
        with pytest.raises(HTTPException) as exc2:
            car_manage.insert_car_for_vendor(
                db4, _create_body(carRegNo="SK01F0001", ownerName="Vendor A"), VENDOR_A
            )
        assert exc2.value.status_code == 409
        assert exc2.value.detail == "CAR_ALREADY_EXISTS"

        # Different vendor, same registration — still conflicts.
        db5 = Session()
        with pytest.raises(HTTPException) as exc3:
            car_manage.insert_car_for_vendor(
                db5, _create_body(carRegNo="SK01F0001", ownerName="Vendor B"), VENDOR_B
            )
        assert exc3.value.status_code == 409
        assert exc3.value.detail == "CAR_ALREADY_EXISTS"


def test_create_car_future_year_rejected():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    db.close()

    future_year = datetime.now(ZoneInfo("Asia/Kolkata")).year + 5
    body = _create_body(carRegNo="SK01G0001", modelYear=future_year)
    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert exc.value.status_code == 422
    assert exc.value.detail == "ERROR_INVALID_MODELYEAR"


def test_create_car_non_numeric_model_year_rejected_by_schema():
    with pytest.raises(Exception):
        _create_body(carRegNo="SK01G0002", modelYear="20+ Years Old")


def test_create_car_invalid_ctd_422():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(carRegNo="SK01H0001", CTD=999)
    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert exc.value.status_code == 422
    assert exc.value.detail == "ERROR_INVALID_CTD"


def test_create_car_owner_same_vendor_no_poa_required():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Ram Kumar")
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(
        carRegNo="SK01I0001",
        ownerName="ram   kumar",
    )
    db2 = Session()
    with patch.object(car_manage, "azure_blob_upload", side_effect=_mock_upload_ok), \
         patch.object(car_manage, "send_email", return_value={"message": "SENT"}):
        result = car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert result.message == "INSERTED"

    row = db2.query(CarDetail).filter(CarDetail.normalizedCarRegNo == "SK01I0001").one()
    assert row.carOwnedBySameVendor is True
    assert row.powerOfAttorneyDoc is None


def test_create_car_owner_differs_requires_poa_then_succeeds_with_poa():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1)
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.insert_car_for_vendor(
            db2,
            _create_body(carRegNo="SK01J0001", ownerName="Someone Else"),
            VENDOR_A,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "ERROR_MISSING_IMAGEPOWEROFATTORNEY"

    db3 = Session()
    with patch.object(car_manage, "azure_blob_upload", side_effect=_mock_upload_ok), \
         patch.object(car_manage, "send_email", return_value={"message": "SENT"}):
        result = car_manage.insert_car_for_vendor(
            db3,
            _create_body(
                carRegNo="SK01J0001",
                ownerName="Someone Else",
                imagePowerOfAttorney=_PNG_B64,
            ),
            VENDOR_A,
        )
    assert result.message == "INSERTED"
    row = db3.query(CarDetail).filter(CarDetail.normalizedCarRegNo == "SK01J0001").one()
    assert row.carOwnedBySameVendor is False
    assert row.powerOfAttorneyDoc is not None


def test_create_car_missing_rc_front_side_required():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.insert_car_for_vendor(
            db2,
            _create_body(carRegNo="SK01K0001", imageVehicleRC=""),
            VENDOR_A,
        )
    assert exc.value.detail == "ERROR_MISSING_IMAGEVEHICLERC"

    db3 = Session()
    with pytest.raises(HTTPException) as exc2:
        car_manage.insert_car_for_vendor(
            db3,
            _create_body(carRegNo="SK01K0002", imageVehicleFront=""),
            VENDOR_A,
        )
    assert exc2.value.detail == "ERROR_MISSING_IMAGEVEHICLEFRONT"

    db4 = Session()
    with pytest.raises(HTTPException) as exc3:
        car_manage.insert_car_for_vendor(
            db4,
            _create_body(carRegNo="SK01K0003", imageVehicleSide=""),
            VENDOR_A,
        )
    assert exc3.value.detail == "ERROR_MISSING_IMAGEVEHICLESIDE"


def test_create_car_invalid_media_rejected_422():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(carRegNo="SK01L0001")
    db2 = Session()
    with patch.object(car_manage, "azure_blob_upload", return_value=(False, "INVALID_BASE64")):
        with pytest.raises(HTTPException) as exc:
            car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert exc.value.status_code == 422
    assert exc.value.detail == "ERROR_INVALID_MEDIA"
    assert db2.query(CarDetail).count() == 0


def test_create_car_media_too_large_mapped_to_422():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(carRegNo="SK01M0001")
    db2 = Session()
    with patch.object(car_manage, "azure_blob_upload", return_value=(False, "FILE_TOO_LARGE")):
        with pytest.raises(HTTPException) as exc:
            car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert exc.value.status_code == 422
    assert exc.value.detail == "ERROR_MEDIA_TOO_LARGE"
    assert db2.query(CarDetail).count() == 0


def test_create_car_rollback_cleans_uploaded_blobs_on_later_failure():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(carRegNo="SK01N0001")

    def _upload(**kwargs):
        if kwargs["blob_name"].endswith("VehicleSide"):
            return False, "INVALID_BASE64"
        return _mock_upload_ok(**kwargs)

    db2 = Session()
    with patch.object(car_manage, "azure_blob_upload", side_effect=_upload), \
         patch.object(car_manage, "azure_blob_delete_by_url") as mock_delete:
        with pytest.raises(HTTPException) as exc:
            car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert exc.value.status_code == 422
    assert mock_delete.call_count >= 1
    assert db2.query(CarDetail).count() == 0


def test_create_car_email_failure_does_not_undo_insert():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1)
    db.close()

    body = _create_body(carRegNo="SK01O0001")
    db2 = Session()
    with patch.object(car_manage, "azure_blob_upload", side_effect=_mock_upload_ok), \
         patch.object(car_manage, "send_email", side_effect=Exception("smtp down")):
        result = car_manage.insert_car_for_vendor(db2, body, VENDOR_A)
    assert result.message == "INSERTED"
    assert db2.query(CarDetail).filter(CarDetail.normalizedCarRegNo == "SK01O0001").count() == 1


def test_create_car_http_422_for_invalid_model_year_and_ctd():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    db.close()

    client = _pr15_client(engine, Session, VENDOR_A)

    resp = client.post(
        "/addcartoprofile",
        json={
            "carRegNo": "SK01P0001",
            "carModel": "Swift",
            "CTD": 1,
            "carColor": "White",
            "modelYear": "20+ Years Old",
            "ownerName": "Vendor A",
            "imageVehicleRC": _PNG_B64,
            "imageVehicleFront": _PNG_B64,
            "imageVehicleSide": _PNG_B64,
        },
    )
    assert resp.status_code == 422

    resp2 = client.post(
        "/addcartoprofile",
        json={
            "carRegNo": "SK01P0002",
            "carModel": "Swift",
            "CTD": "not-an-int",
            "carColor": "White",
            "modelYear": 2022,
            "ownerName": "Vendor A",
            "imageVehicleRC": _PNG_B64,
            "imageVehicleFront": _PNG_B64,
            "imageVehicleSide": _PNG_B64,
        },
    )
    assert resp2.status_code == 422


# ---------------------------------------------------------------------------
# Delete — PUT /deletecarfromprofile
# ---------------------------------------------------------------------------


def test_delete_car_owner_soft_deletes_unused_car():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01Q0001", admin_approved=True)
    car_id = car.CARID
    db.close()

    db2 = Session()
    result = car_manage.delete_car_for_vendor(
        db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
    )
    assert result.message == "DELETED"

    db3 = Session()
    row = db3.query(CarDetail).filter(CarDetail.CARID == car_id).one()
    assert row.isDeleted is True
    assert row.deletedBy == VENDOR_A
    assert row.deletedAt is not None
    assert row.CARID == car_id


def test_delete_car_wrong_owner_403():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01R0001", admin_approved=True)
    car_id = car.CARID
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.delete_car_for_vendor(
            db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_B
        )
    assert exc.value.status_code == 403


def test_delete_car_missing_404():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.delete_car_for_vendor(
            db2, DeleteVendorCarRequest(CARID=99999), VENDOR_A
        )
    assert exc.value.status_code == 404


def test_delete_car_already_deleted_404():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(
        db,
        user_app_id=VENDOR_A,
        reg="SK01S0001",
        admin_approved=True,
        is_deleted=True,
        deleted_by=VENDOR_A,
        deleted_at=datetime(2026, 1, 2),
    )
    car_id = car.CARID
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.delete_car_for_vendor(
            db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
        )
    assert exc.value.status_code == 404


def test_delete_car_blocked_by_open_bid_409():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01T0001", admin_approved=True)
    req = _seed_request(db, status="BID - OPEN")
    _seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - OPEN", car_id=car.CARID)
    car_id = car.CARID
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.delete_car_for_vendor(
            db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "CAR_IN_ACTIVE_USE"

    db3 = Session()
    row = db3.query(CarDetail).filter(CarDetail.CARID == car_id).one()
    assert row.isDeleted is False


def test_delete_car_blocked_by_confirmed_bid_409():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01U0001", admin_approved=True)
    req = _seed_request(db, status="BID - CONFIRMED")
    _seed_bid(
        db, rid=req.RID, bidder_id=VENDOR_A, amount=1000, status="BID - CONFIRMED", car_id=car.CARID
    )
    car_id = car.CARID
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.delete_car_for_vendor(
            db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "CAR_IN_ACTIVE_USE"


def test_delete_car_blocked_by_confirmed_request_409():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01V0001", admin_approved=True)
    req = _seed_request(db, status="REQUEST - CONFIRMED", request_won_by=VENDOR_A)
    _seed_bid(
        db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="REQUEST - CONFIRMED",
        car_id=car.CARID,
    )
    car_id = car.CARID
    db.close()

    db2 = Session()
    with pytest.raises(HTTPException) as exc:
        car_manage.delete_car_for_vendor(
            db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "CAR_IN_ACTIVE_USE"


def test_delete_car_cancelled_bid_status_does_not_block():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01W0001", admin_approved=True)
    req = _seed_request(db, status="REQUEST - CANCELLED")
    _seed_bid(
        db,
        rid=req.RID,
        bidder_id=VENDOR_A,
        amount=1000,
        status="REQUEST - CANCELLED",
        car_id=car.CARID,
    )
    car_id = car.CARID
    db.close()

    db2 = Session()
    result = car_manage.delete_car_for_vendor(
        db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
    )
    assert result.message == "DELETED"

    db3 = Session()
    row = db3.query(CarDetail).filter(CarDetail.CARID == car_id).one()
    assert row.isDeleted is True


def test_delete_car_disappears_from_management_and_pr11_list():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01X0001", admin_approved=True)
    car_id = car.CARID
    db.close()

    db2 = Session()
    result = car_manage.delete_car_for_vendor(
        db2, DeleteVendorCarRequest(CARID=car_id), VENDOR_A
    )
    assert result.message == "DELETED"

    session = Session()
    managed = car_manage.get_managed_cars_for_vendor(session, VENDOR_A)
    assert all(c.CARID != car_id for c in managed)

    session2 = Session()
    lean = vendor_bid_mod.get_vendor_cars_for_bidding(session2, user_id=VENDOR_A)
    assert all(c.CARID != car_id for c in lean)


# ---------------------------------------------------------------------------
# PR11 bid validation — soft-deleted / pending / wrong-vendor cars
# ---------------------------------------------------------------------------


def test_bid_rejects_soft_deleted_car():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(
        db,
        user_app_id=VENDOR_A,
        reg="SK01Y0001",
        admin_approved=True,
        is_deleted=True,
        deleted_by=VENDOR_A,
        deleted_at=datetime(2026, 1, 2),
    )
    req = _seed_request(db, status="BID - OPEN")
    car_id = car.CARID
    rid = req.RID
    db.close()

    db2 = Session()
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            db2,
            bid_data=VendorBidInsert(RID=rid, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


def test_bid_rejects_pending_car():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK01Z0001", admin_approved=False)
    req = _seed_request(db, status="BID - OPEN")
    car_id = car.CARID
    rid = req.RID
    db.close()

    db2 = Session()
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            db2,
            bid_data=VendorBidInsert(RID=rid, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 409


def test_bid_rejects_wrong_vendor_car():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _add_user(db, user_app_id=VENDOR_B, uid=2)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_B, reg="SK02A0001", admin_approved=True)
    req = _seed_request(db, status="BID - OPEN")
    car_id = car.CARID
    rid = req.RID
    db.close()

    db2 = Session()
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        vendor_bid_mod.insert_vendor_bid(
            db2,
            bid_data=VendorBidInsert(RID=rid, CARID=car_id, bidAmount=1200),
            user_id=VENDOR_A,
            background_tasks=bg,
        )
    assert exc.value.status_code == 403


def test_bid_allows_approved_active_owned_car_smoke():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1)
    _seed_ctd(db, ctd=1)
    car = _seed_car(db, user_app_id=VENDOR_A, reg="SK02B0001", admin_approved=True)
    req = _seed_request(db, status="BID - OPEN")
    car_id = car.CARID
    rid = req.RID
    db.close()

    db2 = Session()
    bg = BackgroundTasks()
    result = vendor_bid_mod.insert_vendor_bid(
        db2,
        bid_data=VendorBidInsert(RID=rid, CARID=car_id, bidAmount=1200),
        user_id=VENDOR_A,
        background_tasks=bg,
    )
    assert result.message == "INSERTED"


# ---------------------------------------------------------------------------
# HTTP smoke
# ---------------------------------------------------------------------------


def test_http_smoke_manage_list_create_delete_and_car_types():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_A, uid=1, full_name="Vendor A")
    _seed_ctd(db, ctd=1, manufacturer="Maruti", model="Swift")
    db.close()

    client = _pr15_client(engine, Session, VENDOR_A)

    types_resp = client.get("/getallvendorcartypes")
    assert types_resp.status_code == 200
    assert len(types_resp.json()) == 1

    with patch.object(car_manage, "azure_blob_upload", side_effect=_mock_upload_ok), \
         patch.object(car_manage, "send_email", return_value={"message": "SENT"}), \
         patch.object(car_manage, "azure_blob_delete_by_url", return_value=True):
        create = client.post(
            "/addcartoprofile",
            json={
                "carRegNo": "SK02C0001",
                "carModel": "Swift",
                "CTD": 1,
                "carColor": "White",
                "modelYear": 2022,
                "ownerName": "Vendor A",
                "imageVehicleRC": _PNG_B64,
                "imageVehicleFront": _PNG_B64,
                "imageVehicleSide": _PNG_B64,
            },
        )
        assert create.status_code == 200
        assert create.json()["message"] == "INSERTED"

        listed = client.get("/viewmanagedcarsforvendor")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        car_id = listed.json()[0]["CARID"]

        cars_for_bidding = client.get("/viewcarsforvendor")
        assert cars_for_bidding.status_code == 200
        assert cars_for_bidding.json() == []  # not yet admin-approved

        delete = client.put("/deletecarfromprofile", json={"CARID": car_id})
        assert delete.status_code == 200
        assert delete.json()["message"] == "DELETED"

        listed2 = client.get("/viewmanagedcarsforvendor")
        assert listed2.json() == []
