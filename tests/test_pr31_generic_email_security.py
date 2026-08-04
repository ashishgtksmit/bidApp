"""
PR31 — generic FastAPI POST /sendemail security hardening.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")
# Do not overwrite INTERNAL_NOTIFICATION_KEY at import time — other suites
# (PR25+) share the process env and set their own expected key first.
os.environ.setdefault("INTERNAL_NOTIFICATION_KEY", "pr31-test-internal-key")
os.environ.setdefault("INTERNAL_EMAIL_FROM", "customersupport@wizzride.com")
os.environ.setdefault("INTERNAL_EMAIL_ALLOWED_RECIPIENTS", "ops@example.com")
os.environ.setdefault("INTERNAL_EMAIL_ALLOWED_DOMAINS", "wizzride.com")

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.auth import internal as internal_auth  # noqa: E402
from app_v1.auth import jwt as jwt_mod  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.endpoints import utils as utils_mod  # noqa: E402
from app_v1.endpoints import location as location_mod  # noqa: E402
from app_v1.services import internal_email as email_svc  # noqa: E402
from app_v1.utils import email as email_mod  # noqa: E402
from app_v1.utils import rate_limit as rate_limit_mod  # noqa: E402
from app_v1.utils.common import InternalEmailSendRequest  # noqa: E402

INTERNAL_KEY = "pr31-test-internal-key"
USER_ID = "7022359323"
VENDOR_ID = "8637554388"
TOMBSTONE_ID = "7022359323.DELETED"
ALLOWED_TO = "ops@example.com"
DOMAIN_TO = "alerts@wizzride.com"



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
        tables=[User.__table__, ApiRateLimitBucket.__table__],
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
def _email_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_NOTIFICATION_KEY", INTERNAL_KEY)
    monkeypatch.setenv("INTERNAL_EMAIL_FROM", "customersupport@wizzride.com")
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_RECIPIENTS", ALLOWED_TO)
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_DOMAINS", "wizzride.com")


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
    app.include_router(utils_mod.router)
    app.include_router(location_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(USER_ID)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client_vendor(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: VENDOR_ID
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(VENDOR_ID)
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
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client_real_jwt(engine):
    """Uses real JWT dependency (no override)."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


_UID_SEQ = 2000


def _seed_user(
    db,
    user_app_id: str,
    *,
    lock_app: bool = False,
    also_vendor: bool = False,
) -> User:
    global _UID_SEQ
    _UID_SEQ += 1
    user = User(
        UID=_UID_SEQ,
        userAppId=user_app_id,
        password="x",
        fullName="Test User",
        emailId=f"{user_app_id}@example.com",
        dob="01-01-1990",
        city="Gangtok",
        gender="Male",
        alsoVendor=also_vendor,
        vendorApproved=also_vendor,
        lockApp=lock_app,
        rating="5",
        totalNoOfReviews=0,
        fcmToken="fcm",
        user_login_status="LOGGEDIN",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth_headers(internal_key: str | None = INTERNAL_KEY) -> dict:
    headers = {"Authorization": "Bearer test-jwt"}
    if internal_key is not None:
        headers[internal_auth.INTERNAL_NOTIFICATION_HEADER] = internal_key
    return headers


def _valid_body(**overrides):
    body = {
        "purpose": "OPERATIONS",
        "toAddress": ALLOWED_TO,
        "subject": "Ops check",
        "message": "Hello internal mail",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def seeded(db_session):
    _seed_user(db_session, USER_ID)
    _seed_user(db_session, VENDOR_ID, also_vendor=True)
    return db_session


@pytest.fixture()
def mock_sent():
    with patch.object(
        email_svc,
        "send_email",
        return_value={"message": "SENT", "used_fallback": False},
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Authentication and internal authorization
# ---------------------------------------------------------------------------


def test_01_missing_jwt_401(client_no_jwt):
    r = client_no_jwt.post("/sendemail", json=_valid_body())
    assert r.status_code in (401, 403)


def test_02_invalid_jwt_401(client_real_jwt):
    r = client_real_jwt.post(
        "/sendemail",
        json=_valid_body(),
        headers={
            "Authorization": "Bearer not-a-valid-jwt",
            internal_auth.INTERNAL_NOTIFICATION_HEADER: INTERNAL_KEY,
        },
    )
    assert r.status_code == 401


def test_03_customer_jwt_without_internal_key_403(client, seeded):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers(None))
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_EMAIL_ACCESS_REQUIRED"


def test_04_vendor_jwt_without_internal_key_403(client_vendor, db_session):
    _seed_user(db_session, VENDOR_ID, also_vendor=True)
    r = client_vendor.post("/sendemail", json=_valid_body(), headers=_auth_headers(None))
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_EMAIL_ACCESS_REQUIRED"


def test_05_missing_internal_key_403(client, seeded):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers(None))
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_EMAIL_ACCESS_REQUIRED"


def test_06_invalid_internal_key_403(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(),
        headers=_auth_headers("wrong-key"),
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_EMAIL_ACCESS_REQUIRED"


def test_07_unset_server_secret_fail_closed(client, seeded, monkeypatch):
    monkeypatch.delenv("INTERNAL_NOTIFICATION_KEY", raising=False)
    monkeypatch.setenv("INTERNAL_NOTIFICATION_KEY", "")
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_EMAIL_ACCESS_REQUIRED"


def test_08_valid_jwt_and_key_reaches_delivery(client, seeded, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert r.json() == {"message": "SENT"}
    mock_sent.assert_called_once()


def test_09_missing_user_404(client, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 404
    assert r.json()["detail"] == "USER_NOT_FOUND"
    mock_sent.assert_not_called()


def test_10_tombstoned_user_rejected(client, db_session, engine, mock_sent):
    _seed_user(db_session, TOMBSTONE_ID, lock_app=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: TOMBSTONE_ID
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(TOMBSTONE_ID)
    with TestClient(app) as c:
        r = c.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 404
    assert r.json()["detail"] == "USER_NOT_FOUND"
    mock_sent.assert_not_called()


def test_11_locked_user_403(client, db_session, mock_sent):
    _seed_user(db_session, USER_ID, lock_app=True)
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "ACCOUNT_LOCKED"
    mock_sent.assert_not_called()


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_12_sendemail_absent_from_openapi(client):
    schema = client.app.openapi()
    assert "/sendemail" not in schema["paths"]


def test_13_location_reports_present(client):
    schema = client.app.openapi()
    assert "/location-reports" in schema["paths"]


def test_14_vendor_and_dedicated_routes_remain_visible():
    """PR16/vendor routes stay in public schema when their routers are mounted."""
    from app_v1.endpoints import user as user_mod

    app = FastAPI()
    app.include_router(user_mod.router)
    app.include_router(location_mod.router)
    schema = app.openapi()
    paths = schema["paths"]
    assert "/location-reports" in paths
    assert "/registernewvendor" in paths or "/alsovendorupdate" in paths
    utils_app = FastAPI()
    utils_app.include_router(utils_mod.router)
    utils_schema = utils_app.openapi()
    assert "/sendemail" not in utils_schema["paths"]
    assert "/notificationtodriver" in utils_schema["paths"]


def test_15_no_public_internal_email_schema(client):
    schema = client.app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    assert "InternalEmailSendRequest" not in components
    assert "/sendemail" not in schema["paths"]


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


def test_16_valid_request_accepted(client, seeded, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200


def test_17_missing_purpose_422(client, seeded):
    body = _valid_body()
    del body["purpose"]
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_18_invalid_purpose_422(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(purpose="BROADCAST_ALL"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_19_missing_recipient_422(client, seeded):
    body = _valid_body()
    del body["toAddress"]
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_20_missing_subject_422(client, seeded):
    body = _valid_body()
    del body["subject"]
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_21_missing_message_422(client, seeded):
    body = _valid_body()
    del body["message"]
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"fromAddress": "customersupport@wizzride.com"},
        {"ccAddress": "cc@example.com"},
        {"bccAddress": "bcc@example.com"},
        {"attachmentPath": "/tmp/x.pdf"},
        {"template": "welcome"},
        {"userAppId": USER_ID},
        {"internalKey": INTERNAL_KEY},
        {"isHtml": True},
        {"from_name": "X"},
    ],
)
def test_22_30_extras_forbidden(client, seeded, extra):
    body = _valid_body()
    body.update(extra)
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Recipient policy
# ---------------------------------------------------------------------------


def test_31_explicit_allowlisted_address(client, seeded, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200


def test_32_approved_domain(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(toAddress=DOMAIN_TO),
        headers=_auth_headers(),
    )
    assert r.status_code == 200


def test_33_arbitrary_external_rejected(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(toAddress="attacker@evil.example"),
        headers=_auth_headers(),
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_EMAIL_RECIPIENT_NOT_ALLOWED"
    mock_sent.assert_not_called()


def test_34_empty_allow_lists_fail_closed(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_RECIPIENTS", "")
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_DOMAINS", "")
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert r.json()["detail"] == "INTERNAL_EMAIL_CONFIGURATION_INVALID"
    mock_sent.assert_not_called()


def test_35_invalid_allow_list_config_fail_closed(
    client, seeded, mock_sent, monkeypatch
):
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_RECIPIENTS", "not-an-email")
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_DOMAINS", "")
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert r.json()["detail"] == "INTERNAL_EMAIL_CONFIGURATION_INVALID"


def test_36_case_normalized_domain(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_RECIPIENTS", "")
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_DOMAINS", "WizzRide.COM")
    r = client.post(
        "/sendemail",
        json=_valid_body(toAddress="Ops@WIZZRide.com"),
        headers=_auth_headers(),
    )
    assert r.status_code == 200


def test_37_whitespace_in_config(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setenv(
        "INTERNAL_EMAIL_ALLOWED_RECIPIENTS",
        f"  {ALLOWED_TO} , ",
    )
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_DOMAINS", "  wizzride.com , ")
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200


def test_38_recipient_not_leaked_in_error(client, seeded):
    secret = "leak-check@evil.example"
    r = client.post(
        "/sendemail",
        json=_valid_body(toAddress=secret),
        headers=_auth_headers(),
    )
    assert r.status_code == 403
    body = r.text
    assert secret not in body
    assert "evil.example" not in body


# ---------------------------------------------------------------------------
# Sender policy
# ---------------------------------------------------------------------------


def test_39_configured_sender_used(client, seeded, mock_sent):
    client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    kwargs = mock_sent.call_args.kwargs
    assert kwargs["from_address"] == "customersupport@wizzride.com"


def test_40_missing_sender_uses_fallback(client, seeded, mock_sent, monkeypatch):
    monkeypatch.delenv("INTERNAL_EMAIL_FROM", raising=False)
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert mock_sent.call_args.kwargs["from_address"] == "customersupport@wizzride.com"


def test_41_unsupported_sender_503(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setenv("INTERNAL_EMAIL_FROM", "ticketdetails@wizzride.com")
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert r.json()["detail"] == "INTERNAL_EMAIL_CONFIGURATION_INVALID"
    mock_sent.assert_not_called()


def test_42_client_cannot_select_sender(client, seeded, mock_sent):
    body = _valid_body(fromAddress="reservations@wizzride.com")
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 422
    mock_sent.assert_not_called()


def test_43_sender_not_returned(client, seeded, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert "customersupport" not in r.text
    assert "from" not in r.json()


# ---------------------------------------------------------------------------
# Subject / message validation
# ---------------------------------------------------------------------------


def test_44_empty_subject_rejected(client, seeded):
    r = client.post(
        "/sendemail", json=_valid_body(subject=""), headers=_auth_headers()
    )
    assert r.status_code == 422


def test_45_whitespace_subject_rejected(client, seeded):
    r = client.post(
        "/sendemail", json=_valid_body(subject="   "), headers=_auth_headers()
    )
    assert r.status_code == 422


def test_46_subject_too_long_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="x" * 201),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_47_subject_crlf_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="hi\r\nBcc: evil@x.com"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_48_subject_null_byte_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="hi\x00there"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_49_header_injection_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="Subject\nInjected"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_50_unicode_subject_accepted(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="परीक्षा subject ✓"),
        headers=_auth_headers(),
    )
    assert r.status_code == 200


def test_51_empty_message_rejected(client, seeded):
    r = client.post(
        "/sendemail", json=_valid_body(message=""), headers=_auth_headers()
    )
    assert r.status_code == 422


def test_52_whitespace_message_rejected(client, seeded):
    r = client.post(
        "/sendemail", json=_valid_body(message=" \n\t "), headers=_auth_headers()
    )
    assert r.status_code == 422


def test_53_message_too_long_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="m" * 20001),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_54_unicode_message_accepted(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="नमस्ते world"),
        headers=_auth_headers(),
    )
    assert r.status_code == 200


def test_55_multiline_accepted(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="line1\nline2\nline3"),
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    assert "line1\nline2" in mock_sent.call_args.kwargs["message"]


def test_56_57_html_and_script_treated_as_text(client, seeded, mock_sent):
    payload = '<script>alert(1)</script><b>bold</b>'
    r = client.post(
        "/sendemail",
        json=_valid_body(message=payload),
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    kwargs = mock_sent.call_args.kwargs
    assert kwargs["is_html"] is False
    assert kwargs["message"] == payload


def test_58_message_null_byte_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="hi\x00"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_59_disallowed_controls_rejected(client, seeded):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="hi\x07there"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_60_urls_allowed_as_text(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="See https://example.com/path"),
        headers=_auth_headers(),
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# HTML / plain-text / helper behaviour
# ---------------------------------------------------------------------------


def test_61_63_generic_route_plain_text(client, seeded, mock_sent):
    r = client.post(
        "/sendemail",
        json=_valid_body(message="a\nb"),
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    kwargs = mock_sent.call_args.kwargs
    assert kwargs["is_html"] is False
    assert kwargs["cc_address"] is None
    assert kwargs["bcc_address"] is None
    assert kwargs["attachment_path"] is None
    assert "\n" in kwargs["message"]


def test_64_pr29_html_default_unchanged():
    sig = inspect.signature(email_mod.send_email)
    assert sig.parameters["is_html"].default is True


def test_65_send_email_html_mode_default():
    with patch.object(email_mod.smtplib, "SMTP") as smtp_cls:
        server = smtp_cls.return_value.__enter__.return_value
        server.send_message.return_value = {}
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
                to_address="ops@example.com",
                to_name="Ops",
            )
    assert result["message"] == "SENT"


def test_66_70_attachment_cc_bcc_rejected_and_not_passed(client, seeded, mock_sent):
    for extra in (
        {"attachmentPath": "/etc/passwd"},
        {"ccAddress": "cc@x.com"},
        {"bccAddress": "bcc@x.com"},
        {"attachment": "x"},
        {"filename": "x.pdf"},
    ):
        body = _valid_body()
        body.update(extra)
        r = client.post("/sendemail", json=body, headers=_auth_headers())
        assert r.status_code == 422
    # Valid path never opens filesystem for attachments
    with patch("builtins.open", side_effect=AssertionError("must not open")):
        r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
        assert r.status_code == 200
    kwargs = mock_sent.call_args.kwargs
    assert kwargs.get("attachment_path") is None
    assert kwargs.get("cc_address") is None
    assert kwargs.get("bcc_address") is None


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def test_71_per_minute_caller_limit(client, seeded, mock_sent):
    for i in range(5):
        r = client.post(
            "/sendemail",
            json=_valid_body(subject=f"m{i}", message=f"body {i}"),
            headers=_auth_headers(),
        )
        assert r.status_code == 200
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="m6", message="body 6"),
        headers=_auth_headers(),
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "INTERNAL_EMAIL_RATE_LIMITED"


def test_72_73_hour_and_day_limits_configured():
    assert email_svc._CALLER_PER_HOUR == 30
    assert email_svc._CALLER_PER_DAY == 100


def test_74_exact_recipient_limit(client, seeded, mock_sent, monkeypatch):
    # Raise caller limits so recipient bucket trips first.
    monkeypatch.setattr(email_svc, "_CALLER_PER_MIN", 1000)
    monkeypatch.setattr(email_svc, "_CALLER_PER_HOUR", 1000)
    monkeypatch.setattr(email_svc, "_CALLER_PER_DAY", 1000)
    monkeypatch.setattr(email_svc, "_DOMAIN_PER_HOUR", 1000)
    for i in range(10):
        r = client.post(
            "/sendemail",
            json=_valid_body(subject=f"r{i}", message=f"rb{i}"),
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="r11", message="rb11"),
        headers=_auth_headers(),
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "INTERNAL_EMAIL_RATE_LIMITED"


def test_75_recipient_domain_limit(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setattr(email_svc, "_CALLER_PER_MIN", 1000)
    monkeypatch.setattr(email_svc, "_CALLER_PER_HOUR", 1000)
    monkeypatch.setattr(email_svc, "_CALLER_PER_DAY", 1000)
    monkeypatch.setattr(email_svc, "_RECIPIENT_PER_HOUR", 1000)
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_RECIPIENTS", "")
    monkeypatch.setenv("INTERNAL_EMAIL_ALLOWED_DOMAINS", "wizzride.com")
    for i in range(20):
        r = client.post(
            "/sendemail",
            json=_valid_body(
                toAddress=f"user{i}@wizzride.com",
                subject=f"d{i}",
                message=f"db{i}",
            ),
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
    r = client.post(
        "/sendemail",
        json=_valid_body(
            toAddress="user21@wizzride.com",
            subject="d21",
            message="db21",
        ),
        headers=_auth_headers(),
    )
    assert r.status_code == 429


def test_76_different_callers_separate_buckets(client, seeded, engine, mock_sent):
    for i in range(5):
        assert (
            client.post(
                "/sendemail",
                json=_valid_body(subject=f"c{i}", message=f"cb{i}"),
                headers=_auth_headers(),
            ).status_code
            == 200
        )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: VENDOR_ID
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(VENDOR_ID)
    with TestClient(app) as c:
        r = c.post(
            "/sendemail",
            json=_valid_body(subject="vendor", message="vendor body"),
            headers=_auth_headers(),
        )
    assert r.status_code == 200


def test_77_different_recipients_separate_buckets(
    client, seeded, mock_sent, monkeypatch
):
    monkeypatch.setattr(email_svc, "_CALLER_PER_MIN", 1000)
    monkeypatch.setenv(
        "INTERNAL_EMAIL_ALLOWED_RECIPIENTS",
        f"{ALLOWED_TO},other@example.com",
    )
    for i in range(10):
        assert (
            client.post(
                "/sendemail",
                json=_valid_body(subject=f"a{i}", message=f"ab{i}"),
                headers=_auth_headers(),
            ).status_code
            == 200
        )
    r = client.post(
        "/sendemail",
        json=_valid_body(
            toAddress="other@example.com",
            subject="other",
            message="other body",
        ),
        headers=_auth_headers(),
    )
    assert r.status_code == 200


def test_78_hashed_bucket_identifiers():
    assert email_svc._hash_part("x") == email_svc._hash_part("x")
    assert "@" not in email_svc._hash_part(ALLOWED_TO)
    assert ALLOWED_TO not in email_svc._hash_part(ALLOWED_TO)


def test_79_rate_limiter_db_failure_fail_closed(client, seeded, monkeypatch):
    def _boom(*args, **kwargs):
        return rate_limit_mod.ErrorResponse(message="RATE_LIMITED")

    # Simulate fail_closed path returning RATE_LIMITED
    monkeypatch.setattr(email_svc, "enforce_rate_limit", _boom)
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 429
    assert r.json()["detail"] == "INTERNAL_EMAIL_RATE_LIMITED"


def test_79b_fail_closed_flag_on_sqlalchemy_error(db_session):
    with patch.object(
        db_session,
        "query",
        side_effect=SQLAlchemyError("db down"),
    ):
        result = rate_limit_mod.enforce_rate_limit(
            db_session,
            bucket_key="internal_email:test",
            max_hits=1,
            window_seconds=60,
            fail_closed=True,
        )
    assert result is not None
    assert result.message == "RATE_LIMITED"


def test_80_429_safe_code(client, seeded, mock_sent):
    for i in range(5):
        client.post(
            "/sendemail",
            json=_valid_body(subject=f"s{i}", message=f"m{i}"),
            headers=_auth_headers(),
        )
    r = client.post(
        "/sendemail",
        json=_valid_body(subject="s6", message="m6"),
        headers=_auth_headers(),
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "INTERNAL_EMAIL_RATE_LIMITED"
    assert ALLOWED_TO not in r.text


def test_81_auth_failures_do_not_consume_send_buckets(client, seeded, mock_sent):
    for _ in range(5):
        r = client.post(
            "/sendemail",
            json=_valid_body(),
            headers=_auth_headers(None),
        )
        assert r.status_code == 403
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200


def test_82_duplicate_suppression(client, seeded, mock_sent, monkeypatch):
    monkeypatch.setattr(email_svc, "_CALLER_PER_MIN", 1000)
    monkeypatch.setattr(email_svc, "_CALLER_PER_HOUR", 1000)
    monkeypatch.setattr(email_svc, "_RECIPIENT_PER_HOUR", 1000)
    monkeypatch.setattr(email_svc, "_DOMAIN_PER_HOUR", 1000)
    body = _valid_body(subject="dup", message="same body")
    assert client.post("/sendemail", json=body, headers=_auth_headers()).status_code == 200
    r = client.post("/sendemail", json=body, headers=_auth_headers())
    assert r.status_code == 429
    assert r.json()["detail"] == "INTERNAL_EMAIL_DUPLICATE_SUPPRESSED"


# ---------------------------------------------------------------------------
# Logging / security
# ---------------------------------------------------------------------------


def test_83_94_audit_redaction(client, seeded, mock_sent, caplog):
    with caplog.at_level(logging.INFO, logger=email_svc._logger.name):
        r = client.post(
            "/sendemail",
            json=_valid_body(
                subject="Secret Subject Line",
                message="Secret body content that must not log",
            ),
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Bearer" not in joined
    assert INTERNAL_KEY not in joined
    assert "unit-test-jwt-secret" not in joined
    assert "SMTP" not in joined.upper() or "password" not in joined.lower()
    assert ALLOWED_TO not in joined
    assert "Secret Subject Line" not in joined
    assert "Secret body content" not in joined
    assert "internal_email_audit" in joined
    assert "OPERATIONS" in joined
    assert "SENT" in joined


def test_84_jwt_secret_not_printed(capsys):
    src = inspect.getsource(jwt_mod.decode_token)
    assert "print(secret)" not in src
    assert "print(e)" not in src


def test_91_92_provider_exception_not_leaked(client, seeded, caplog):
    with patch.object(
        email_svc,
        "send_email",
        side_effect=RuntimeError("smtp password=hunter2 host=smtp.gmail.com"),
    ):
        with caplog.at_level(logging.WARNING, logger=email_svc._logger.name):
            r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert r.json()["detail"] == "INTERNAL_EMAIL_DELIVERY_FAILED"
    assert "hunter2" not in r.text
    assert "smtp.gmail.com" not in r.text
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "hunter2" not in joined
    assert "smtp.gmail.com" not in joined


def test_93_no_filesystem_path_logged(client, seeded, mock_sent, caplog):
    with caplog.at_level(logging.INFO, logger=email_svc._logger.name):
        client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "/tmp" not in joined
    assert "attachment" not in joined.lower() or "path" not in joined.lower()


def test_94_hashes_deterministic():
    assert email_svc._hash_part("abc") == email_svc._hash_part("abc")
    assert len(email_svc._hash_part("abc")) == 32


# ---------------------------------------------------------------------------
# Delivery behaviour
# ---------------------------------------------------------------------------


def test_95_mock_smtp_success_200(client, seeded, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert r.json() == {"message": "SENT"}


def test_96_helper_failure_safe_503(client, seeded):
    with patch.object(
        email_svc, "send_email", return_value={"message": "ERROR_SENDING_EMAIL"}
    ):
        r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert r.json()["detail"] == "INTERNAL_EMAIL_DELIVERY_FAILED"


def test_97_smtp_exception_safe(client, seeded):
    with patch.object(email_svc, "send_email", side_effect=Exception("boom")):
        r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert "boom" not in r.text


def test_98_fallback_does_not_expose_mailbox(client, seeded):
    with patch.object(
        email_svc,
        "send_email",
        return_value={"message": "SENT", "used_fallback": True},
    ):
        r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert "ticketdetails" not in r.text
    assert "fallback" not in r.text.lower()


def test_99_100_success_message_only(client, seeded, mock_sent):
    r = client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert r.json() == {"message": "SENT"}
    assert set(r.json().keys()) == {"message"}


# ---------------------------------------------------------------------------
# Regression / source guards
# ---------------------------------------------------------------------------


def test_101_104_send_email_signature_compatible():
    sig = inspect.signature(email_mod.send_email)
    params = list(sig.parameters)
    assert "to_address" in params
    assert "cc_address" in params
    assert "bcc_address" in params
    assert "is_html" in params
    assert sig.parameters["is_html"].default is True


def test_105_plain_text_mode_only_generic_route(client, seeded, mock_sent):
    client.post("/sendemail", json=_valid_body(), headers=_auth_headers())
    assert mock_sent.call_args.kwargs["is_html"] is False


def test_106_no_php_changes_in_pr31():
    # Source guard: this FastAPI PR must not touch PHP trees from this package.
    php_hits = list(ROOT.glob("**/*.php"))
    assert php_hits == []


def test_107_no_db_migration_introduced():
    mig = ROOT / "migrations"
    if not mig.exists():
        return
    names = [p.name for p in mig.iterdir() if "pr31" in p.name.lower() or "email_audit" in p.name.lower()]
    assert names == []


def test_108_no_worker_firebase_dependency():
    import ast

    tree = ast.parse(Path(email_svc.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported).lower()
    assert "worker" not in joined
    assert "firebase" not in joined
    assert "backgroundtasks" not in joined


def test_schema_extra_forbid():
    with pytest.raises(Exception):
        InternalEmailSendRequest(
            purpose="OPERATIONS",
            toAddress=ALLOWED_TO,
            subject="s",
            message="m",
            fromAddress="x@y.com",
        )
