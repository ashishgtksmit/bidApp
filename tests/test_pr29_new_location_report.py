"""
PR29 — POST /location-reports (authenticated missing-location report).

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import logging
import os
import sys
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
os.environ["LOCATION_REPORT_EMAIL_TO"] = "ops-location@example.com"
os.environ["LOCATION_REPORT_EMAIL_FROM"] = "customersupport@wizzride.com"
os.environ.pop("LOCATION_REPORT_EMAIL_CC", None)
os.environ.pop("LOCATION_REPORT_EMAIL_BCC", None)

import types

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
_fake_firebase.db = types.ModuleType("firebase_admin.db")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)
sys.modules.setdefault("firebase_admin.db", _fake_firebase.db)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.region_details import Region  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.endpoints import location as location_mod  # noqa: E402
from app_v1.endpoints import utils as utils_mod  # noqa: E402
from app_v1.services import location_reports as report_svc  # noqa: E402
from app_v1.utils import email as email_mod  # noqa: E402

REPORTER_ID = "7022359323"
VENDOR_ID = "8637554388"
OTHER_ID = "9000000001"
REGION_ID = 101
REGION_NAME = "Sikkim East"



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
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            User.__table__,
            Region.__table__,
            LocationDetail.__table__,
            ApiRateLimitBucket.__table__,
        ],
    )
    return eng


@pytest.fixture()
def db_session(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _mail_env(monkeypatch):
    monkeypatch.setenv("LOCATION_REPORT_EMAIL_TO", "ops-location@example.com")
    monkeypatch.setenv("LOCATION_REPORT_EMAIL_FROM", "customersupport@wizzride.com")
    monkeypatch.delenv("LOCATION_REPORT_EMAIL_CC", raising=False)
    monkeypatch.delenv("LOCATION_REPORT_EMAIL_BCC", raising=False)


@pytest.fixture()
def client(engine, db_session):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(location_mod.router)
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: REPORTER_ID
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(REPORTER_ID)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client_no_jwt(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(location_mod.router)
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_client(engine, user_id: str):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(location_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app), app


_UID_SEQ = 5000
_RDID_SEQ = 100
_LOC_SEQ = 7000


def _seed_user(
    db,
    user_app_id: str,
    *,
    full_name: str = "Reporter Name",
    email: str = "reporter@example.com",
    lock_app: bool = False,
    also_vendor: bool = False,
    vendor_approved: bool = False,
) -> User:
    global _UID_SEQ
    _UID_SEQ += 1
    user = User(
        UID=_UID_SEQ,
        userAppId=user_app_id,
        password="x",
        fullName=full_name,
        emailId=email,
        dob="01-01-1990",
        city="Gangtok",
        gender="Male",
        alsoVendor=also_vendor,
        vendorApproved=vendor_approved,
        lockApp=lock_app,
        rating="5",
        totalNoOfReviews=0,
        fcmToken="fcm-secret",
        user_login_status="LOGGEDIN",
        bankAccountNo="1234567890",
        bankIFSC="SBIN0001234",
        imageAadhar="secret-aadhar",
        imagePAN="secret-pan",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_region(db, region_id: int = REGION_ID, name: str = REGION_NAME) -> Region:
    region = Region(RDID=region_id, regionName=name)
    db.add(region)
    db.commit()
    db.refresh(region)
    return region


def _seed_location(db, location_name: str, region_id: int = REGION_ID) -> LocationDetail:
    global _LOC_SEQ
    _LOC_SEQ += 1
    loc = LocationDetail(
        LID=_LOC_SEQ,
        location=location_name,
        regionId=region_id,
    )
    db.add(loc)
    db.commit()
    return loc


def _valid_body(**overrides):
    body = {
        "locationName": "Tathangchen Hill",
        "landmark": "Near Rumtek Monastery gate",
        "regionId": REGION_ID,
        "regionOther": False,
        "usageType": "PICKUP",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def seeded(db_session):
    _seed_user(db_session, REPORTER_ID)
    _seed_region(db_session)
    return db_session


@pytest.fixture()
def mock_sent():
    with patch.object(
        report_svc,
        "send_email",
        return_value={"message": "SENT"},
    ) as mocked:
        yield mocked


# ---------------------------------------------------------------------------
# Auth / lifecycle
# ---------------------------------------------------------------------------


def test_01_missing_jwt_401(client_no_jwt):
    r = client_no_jwt.post("/location-reports", json=_valid_body())
    assert r.status_code in (401, 403)


def test_02_invalid_jwt_401(engine, seeded):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(location_mod.router)
    app.dependency_overrides[get_db] = _override_db
    # No override → HTTPBearer rejects missing/invalid credentials
    with TestClient(app) as c:
        r = c.post(
            "/location-reports",
            json=_valid_body(),
            headers={"Authorization": "Bearer not-a-real-token"},
        )
    assert r.status_code in (401, 403)


def test_03_missing_user_404(client, mock_sent, db_session):
    _seed_region(db_session)
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 404
    assert r.json()["detail"] == "USER_NOT_FOUND"
    mock_sent.assert_not_called()


def test_04_tombstoned_user_rejected(client, mock_sent, db_session, engine):
    _seed_region(db_session)
    _seed_user(db_session, f"{REPORTER_ID}.DELETED")
    c, app = _make_client(engine, f"{REPORTER_ID}.DELETED")
    try:
        r = c.post("/location-reports", json=_valid_body())
        assert r.status_code == 404
        assert r.json()["detail"] == "USER_NOT_FOUND"
        mock_sent.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_05_locked_user_403(client, mock_sent, db_session):
    _seed_region(db_session)
    _seed_user(db_session, REPORTER_ID, lock_app=True)
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 403
    assert r.json()["detail"] == "ACCOUNT_LOCKED"
    mock_sent.assert_not_called()


def test_06_customer_may_submit(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 200
    assert r.json() == {"message": "REPORT_SUBMITTED"}
    mock_sent.assert_called_once()


def test_07_vendor_mode_may_submit(engine, db_session, mock_sent):
    _seed_region(db_session)
    _seed_user(
        db_session,
        VENDOR_ID,
        also_vendor=True,
        vendor_approved=False,
        full_name="Vendor Pending",
    )
    c, app = _make_client(engine, VENDOR_ID)
    try:
        r = c.post("/location-reports", json=_valid_body(usageType="DROP"))
        assert r.status_code == 200
        assert r.json()["message"] == "REPORT_SUBMITTED"
    finally:
        app.dependency_overrides.clear()


def test_08_09_no_client_ownership_or_phone_extra(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(phone=REPORTER_ID, userAppId=REPORTER_ID),
    )
    assert r.status_code == 422
    mock_sent.assert_not_called()


# ---------------------------------------------------------------------------
# Schema / forbidden email control
# ---------------------------------------------------------------------------


def test_10_valid_pickup(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(usageType="PICKUP"))
    assert r.status_code == 200


def test_11_valid_drop(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(usageType="DROP"))
    assert r.status_code == 200


def test_12_invalid_usage_type(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(usageType="BOTH"))
    assert r.status_code == 422
    mock_sent.assert_not_called()


@pytest.mark.parametrize(
    "extra_key,extra_val",
    [
        ("toaddress", "evil@example.com"),
        ("fromaddress", "evil@example.com"),
        ("subject", "Hijacked"),
        ("message", "<b>x</b>"),
        ("ccaddress", "cc@example.com"),
        ("bccaddress", "bcc@example.com"),
        ("template", "x"),
        ("attachment", "x"),
        ("to_address", "evil@example.com"),
        ("from_address", "evil@example.com"),
        ("html", "<script>"),
    ],
)
def test_13_18_extra_email_fields_rejected(
    client, seeded, mock_sent, extra_key, extra_val
):
    body = _valid_body()
    body[extra_key] = extra_val
    r = client.post("/location-reports", json=body)
    assert r.status_code == 422
    mock_sent.assert_not_called()


# ---------------------------------------------------------------------------
# Location validation
# ---------------------------------------------------------------------------


def test_19_unicode_indian_location(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(locationName="गंगटोक बाजार", landmark="मठ के पास"),
    )
    assert r.status_code == 200


def test_20_punctuation_accepted(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(
            locationName="Bagdogra Airport (IXB)",
            landmark="Opp. Khangri Petrol Pump, Tadong / NH-10",
        ),
    )
    assert r.status_code == 200


def test_21_empty_location_rejected(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(locationName=""))
    assert r.status_code == 422


def test_22_whitespace_only_location_rejected(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(locationName="   "))
    assert r.status_code == 422


def test_23_too_short_location_rejected(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(locationName="A"))
    assert r.status_code == 422
    mock_sent.assert_not_called()


def test_24_too_long_location_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(locationName="X" * 121),
    )
    assert r.status_code == 422


def test_25_empty_landmark_rejected(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(landmark=""))
    assert r.status_code == 422


def test_26_too_long_landmark_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(landmark="Y" * 251),
    )
    assert r.status_code == 422


def test_27_control_characters_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(locationName="Bad\x07City"),
    )
    assert r.status_code == 422
    mock_sent.assert_not_called()


def test_28_crlf_injection_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(landmark="Near gate\r\nBcc: evil@example.com"),
    )
    assert r.status_code == 422
    mock_sent.assert_not_called()


def test_29_html_tags_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(locationName="<b>Gangtok</b>"),
    )
    assert r.status_code == 422


def test_30_script_payload_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(landmark="<script>alert(1)</script> near temple"),
    )
    assert r.status_code == 422


def test_31_null_byte_rejected(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(locationName="Gang\x00tok"),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Region validation
# ---------------------------------------------------------------------------


def test_32_33_valid_region_canonical_name(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 200
    kwargs = mock_sent.call_args.kwargs
    assert REGION_NAME in kwargs["message"]
    assert "OpenBid Missing Location Report — Pickup" == kwargs["subject"]


def test_34_missing_region_id_without_other(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(regionId=None, regionOther=False),
    )
    assert r.status_code == 422
    mock_sent.assert_not_called()


def test_35_region_id_with_other_true(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(regionId=REGION_ID, regionOther=True),
    )
    assert r.status_code == 422


def test_36_unknown_region_404(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body(regionId=999999))
    assert r.status_code == 404
    assert r.json()["detail"] == "REGION_NOT_FOUND"
    mock_sent.assert_not_called()


def test_37_region_other_accepted(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(regionId=None, regionOther=True),
    )
    assert r.status_code == 200
    assert "Others" in mock_sent.call_args.kwargs["message"]


def test_38_existing_catalog_location_accepted(client, seeded, mock_sent, db_session):
    _seed_location(db_session, "Tathangchen Hill")
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Email ownership / template
# ---------------------------------------------------------------------------


def test_39_58_email_ownership_and_safety(client, seeded, mock_sent):
    r = client.post(
        "/location-reports",
        json=_valid_body(
            locationName="Namchi View",
            landmark="Near plaza & cafe",
            usageType="DROP",
        ),
    )
    assert r.status_code == 200
    kwargs = mock_sent.call_args.kwargs
    assert kwargs["to_address"] == "ops-location@example.com"
    assert kwargs["from_address"] == "customersupport@wizzride.com"
    assert kwargs["subject"] == "OpenBid Missing Location Report — Drop"
    assert REPORTER_ID not in kwargs["subject"]
    assert "7022359323" not in kwargs["subject"]
    body = kwargs["message"]
    assert "Namchi View" in body
    assert "Near plaza &amp; cafe" in body or "Near plaza & cafe" in body
    assert REGION_NAME in body
    assert "Reporter Name" in body
    assert REPORTER_ID in body
    assert "reporter@example.com" in body
    assert "fcm-secret" not in body
    assert "secret-aadhar" not in body
    assert "1234567890" not in body
    assert "SBIN0001234" not in body
    assert "Drop" in body
    assert "OpenBid mobile application" in body
    # Client cannot influence recipient/subject via body (already 422 tested);
    # assert server used env values only.
    assert kwargs["to_address"] != "support@openbid.live"


def test_50_html_escaped_when_reaching_template():
    # Direct unit: escape path for values that pass validation without tags.
    rendered = report_svc._build_html(
        location_name="A & B",
        landmark="C < D",
        region_name="Others",
        usage_type="PICKUP",
        reporter_name="O'Reilly",
        reporter_phone="1",
        reporter_email=None,
    )
    assert "&amp;" in rendered
    assert "&lt;" in rendered
    assert "<script" not in rendered.lower() or "&lt;script" in rendered.lower()


# ---------------------------------------------------------------------------
# Configuration / SMTP
# ---------------------------------------------------------------------------


def test_51_missing_recipient_config(client, seeded, mock_sent, monkeypatch):
    monkeypatch.delenv("LOCATION_REPORT_EMAIL_TO", raising=False)
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 503
    assert r.json()["detail"] == "LOCATION_REPORT_CONFIGURATION_INVALID"
    mock_sent.assert_not_called()


def test_52_invalid_from_config(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setenv("LOCATION_REPORT_EMAIL_FROM", "not-allowed@example.com")
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 503
    assert r.json()["detail"] == "LOCATION_REPORT_CONFIGURATION_INVALID"
    mock_sent.assert_not_called()


def test_53_smtp_success(client, seeded, mock_sent):
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 200
    assert r.json() == {"message": "REPORT_SUBMITTED"}


def test_54_smtp_helper_error(client, seeded):
    with patch.object(
        report_svc, "send_email", return_value={"message": "ERROR_SENDING_EMAIL"}
    ):
        r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 503
    assert r.json()["detail"] == "LOCATION_REPORT_DELIVERY_FAILED"


def test_55_56_57_58_smtp_exception_safe(client, seeded):
    with patch.object(
        report_svc,
        "send_email",
        side_effect=RuntimeError("SMTP password=secret host=smtp.gmail.com"),
    ):
        r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail == "LOCATION_REPORT_DELIVERY_FAILED"
    body = r.text
    assert "password" not in body.lower()
    assert "smtp.gmail.com" not in body
    assert "ops-location@example.com" not in body
    assert "secret" not in body.lower()


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def test_59_60_sender_hourly_limit(client, seeded, mock_sent):
    for i in range(5):
        r = client.post(
            "/location-reports",
            json=_valid_body(locationName=f"Place {i}", landmark=f"Mark {i}"),
        )
        assert r.status_code == 200, r.text
    r6 = client.post(
        "/location-reports",
        json=_valid_body(locationName="Place six", landmark="Mark six"),
    )
    assert r6.status_code == 429
    assert r6.json()["detail"] == "LOCATION_REPORT_RATE_LIMITED"


def test_61_62_location_daily_and_normalization(client, seeded, mock_sent):
    r1 = client.post(
        "/location-reports",
        json=_valid_body(locationName="  Tathangchen   Hill ", landmark="Gate A"),
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/location-reports",
        json=_valid_body(locationName="tathangchen hill", landmark="Gate B"),
    )
    assert r2.status_code == 429
    assert r2.json()["detail"] == "LOCATION_REPORT_RATE_LIMITED"


def test_63_different_usage_type_separate_bucket(client, seeded, mock_sent):
    r1 = client.post(
        "/location-reports",
        json=_valid_body(locationName="Shared Loc", usageType="PICKUP"),
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/location-reports",
        json=_valid_body(locationName="Shared Loc", usageType="DROP"),
    )
    assert r2.status_code == 200


def test_64_different_user_separate_bucket(engine, db_session, mock_sent):
    _seed_region(db_session)
    _seed_user(db_session, REPORTER_ID)
    _seed_user(db_session, OTHER_ID, email="other@example.com")
    c1, app1 = _make_client(engine, REPORTER_ID)
    c2, app2 = _make_client(engine, OTHER_ID)
    try:
        assert (
            c1.post(
                "/location-reports",
                json=_valid_body(locationName="Same Spot"),
            ).status_code
            == 200
        )
        assert (
            c2.post(
                "/location-reports",
                json=_valid_body(locationName="Same Spot"),
            ).status_code
            == 200
        )
    finally:
        app1.dependency_overrides.clear()
        app2.dependency_overrides.clear()


def test_65_different_region_separate_bucket(client, seeded, mock_sent, db_session):
    _seed_region(db_session, region_id=202, name="West Sikkim")
    r1 = client.post(
        "/location-reports",
        json=_valid_body(locationName="Border Town", regionId=REGION_ID),
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/location-reports",
        json=_valid_body(locationName="Border Town", regionId=202),
    )
    assert r2.status_code == 200


def test_66_validation_failures_do_not_consume_bucket(client, seeded, mock_sent):
    for _ in range(3):
        bad = client.post("/location-reports", json=_valid_body(locationName=""))
        assert bad.status_code == 422
    # Still able to submit up to sender limit
    for i in range(5):
        r = client.post(
            "/location-reports",
            json=_valid_body(locationName=f"Ok Place {i}", landmark=f"Lm {i}"),
        )
        assert r.status_code == 200


def test_67_rate_limit_code_stable(client, seeded, mock_sent):
    client.post("/location-reports", json=_valid_body())
    r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 429
    assert r.json()["detail"] == "LOCATION_REPORT_RATE_LIMITED"


def test_68_db_limiter_fail_open_documented():
    """Shared enforce_rate_limit fail-opens on SQLAlchemyError (returns None)."""
    import inspect

    src = inspect.getsource(report_svc.enforce_rate_limit)
    assert "Fail-open" in src or "fail-open" in src.lower() or "return None" in src


# ---------------------------------------------------------------------------
# Logging / security
# ---------------------------------------------------------------------------


def test_69_73_no_pii_in_service_logs(client, seeded, mock_sent, caplog):
    with caplog.at_level(logging.INFO, logger=report_svc._logger.name):
        r = client.post("/location-reports", json=_valid_body())
    assert r.status_code == 200
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "ops-location@example.com" not in joined
    assert "reporter@example.com" not in joined
    assert "Near Rumtek" not in joined
    assert "Tathangchen" not in joined
    assert "<html" not in joined.lower()
    assert "password" not in joined.lower()


def test_email_helper_does_not_print_recipient(capsys):
    with patch.object(email_mod.smtplib, "SMTP") as smtp_cls:
        server = smtp_cls.return_value.__enter__.return_value
        server.send_message.return_value = {}
        # Need valid env SMTP username/password for login path
        with patch.dict(
            os.environ,
            {
                "SMTP_CUSTOMERSUPPORT_USERNAME": "customersupport@wizzride.com",
                "SMTP_CUSTOMERSUPPORT_PASSWORD": "x",
            },
        ):
            result = email_mod.send_email(
                message="<p>hi</p>",
                subject="t",
                from_address="customersupport@wizzride.com",
                from_name="OpenBid",
                to_address="secret-recipient@example.com",
                to_name="Ops",
            )
    assert result["message"] == "SENT"
    out = capsys.readouterr().out
    assert "secret-recipient@example.com" not in out


# ---------------------------------------------------------------------------
# Contract / regression
# ---------------------------------------------------------------------------


def test_74_75_openapi_includes_route(client):
    schema = client.app.openapi()
    assert "/location-reports" in schema["paths"]
    post = schema["paths"]["/location-reports"]["post"]
    assert "requestBody" in post
    components = schema.get("components", {}).get("schemas", {})
    # Find LocationReportRequest
    req = components.get("LocationReportRequest")
    assert req is not None
    assert req.get("additionalProperties") is False


def test_76_no_new_report_model_table():
    model_dir = Path(report_svc.__file__).resolve().parents[1] / "models"
    names = [p.name for p in model_dir.glob("*location_report*")]
    assert names == []


def test_77_no_worker_import_in_service():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(report_svc.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported)
    assert "BackgroundTasks" not in joined
    assert "worker" not in joined.lower()


def test_78_getregions_unchanged(client, seeded):
    r = client.get("/getregions")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert data[0]["regionId"] == REGION_ID
    assert data[0]["regionName"] == REGION_NAME


def test_79_generic_sendemail_hidden_and_separate_from_location_reports(client):
    """PR31: /sendemail is hidden from OpenAPI; /location-reports remains public."""
    schema = client.app.openapi()
    assert "/sendemail" not in schema["paths"]
    assert "/location-reports" in schema["paths"]
    # Route still exists on the app (restricted), just not in public schema.
    paths = {getattr(r, "path", None) for r in client.app.routes}
    assert "/sendemail" in paths
    assert "/location-reports" in paths


def test_80_send_email_backward_compatible_signature():
    import inspect

    sig = inspect.signature(email_mod.send_email)
    params = list(sig.parameters)
    assert "to_address" in params
    assert "cc_address" in params
    assert "bcc_address" in params
    assert "is_html" in params
    assert sig.parameters["is_html"].default is True
