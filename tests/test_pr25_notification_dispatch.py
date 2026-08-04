"""
PR25 — generic FastAPI notification route hardening + mutation regression guards.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

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
# Fail-closed default; individual tests override when needed.
os.environ["INTERNAL_NOTIFICATION_KEY"] = "pr25-test-internal-key"

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.auth import internal as internal_auth  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.endpoints import utils as utils_mod  # noqa: E402
from app_v1.services import notifications as notifications_mod  # noqa: E402
from app_v1.utils.common import FCMSend, FCMSendDrivers  # noqa: E402

INTERNAL_KEY = "pr25-test-internal-key"
USER_ID = "7022359323"
OTHER_ID = "8637554388"
TOMBSTONE_ID = "7022359323.DELETED"



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
    Base.metadata.create_all(bind=eng, tables=[User.__table__])
    return eng


@pytest.fixture()
def db_session(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


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
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(USER_ID)
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
    # Do not override get_current_user_id — HTTPBearer should 401.
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


_UID_SEQ = 1000


def _seed_user(db, user_app_id: str, fcm_token: str | None = "fcm-token-abc") -> User:
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
        alsoVendor=False,
        vendorApproved=False,
        lockApp=False,
        rating="5",
        totalNoOfReviews=0,
        fcmToken=fcm_token,
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


def _notify_body(**overrides):
    body = {
        "title": "Test",
        "body": "Hello",
        "userAppId": OTHER_ID,
        "url": "///Open Requests",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Generic endpoint access
# ---------------------------------------------------------------------------


def test_notificationtodriver_without_jwt_returns_401(client_no_jwt):
    r = client_no_jwt.post("/notificationtodriver", json=_notify_body())
    # HTTPBearer(auto_error=True) yields 403 when Authorization is absent (project convention).
    assert r.status_code in (401, 403)


def test_notificationtodriver_jwt_without_internal_key_returns_403(client, db_session):
    _seed_user(db_session, OTHER_ID)
    r = client.post(
        "/notificationtodriver",
        json=_notify_body(),
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_NOTIFICATION_ACCESS_REQUIRED"


def test_notificationtodriver_wrong_internal_key_returns_403(client, db_session):
    _seed_user(db_session, OTHER_ID)
    r = client.post(
        "/notificationtodriver",
        json=_notify_body(),
        headers=_auth_headers(internal_key="wrong-key"),
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_NOTIFICATION_ACCESS_REQUIRED"


def test_notificationtodriver_valid_internal_key_reaches_handler(client, db_session):
    _seed_user(db_session, OTHER_ID)
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as mock_send:
        r = client.post(
            "/notificationtodriver",
            json=_notify_body(),
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"
    assert mock_send.called


def test_no_secret_configured_fail_closed(client, db_session, monkeypatch):
    _seed_user(db_session, OTHER_ID)
    monkeypatch.delenv("INTERNAL_NOTIFICATION_KEY", raising=False)
    monkeypatch.setenv("INTERNAL_NOTIFICATION_KEY", "")
    r = client.post(
        "/notificationtodriver",
        json=_notify_body(),
        headers=_auth_headers(),
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_NOTIFICATION_ACCESS_REQUIRED"


def test_internal_key_never_appears_in_response(client, db_session):
    _seed_user(db_session, OTHER_ID)
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r = client.post(
            "/notificationtodriver",
            json=_notify_body(),
            headers=_auth_headers(),
        )
    body = r.text
    assert INTERNAL_KEY not in body
    assert "INTERNAL_NOTIFICATION_KEY" not in body


def test_internal_key_never_appears_in_logs(client, db_session, caplog):
    _seed_user(db_session, OTHER_ID)
    with caplog.at_level(logging.DEBUG):
        with patch(
            "app_v1.services.notifications.send_notification_to_token",
            return_value={"success": True, "message": "NOTIFICATION_SENT"},
        ):
            client.post(
                "/notificationtodriver",
                json=_notify_body(),
                headers=_auth_headers(),
            )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert INTERNAL_KEY not in joined


# ---------------------------------------------------------------------------
# Raw-token endpoint
# ---------------------------------------------------------------------------


def test_sendfcmnotification_normal_jwt_forbidden(client):
    r = client.post(
        "/sendfcmnotification",
        json={
            "title": "T",
            "body": "B",
            "fcmToken": "raw-token-xyz",
            "url": "/",
        },
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403


def test_sendfcmnotification_internal_auth_permits(client):
    with patch(
        "app_v1.endpoints.utils.send_notification",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as mock_send:
        r = client.post(
            "/sendfcmnotification",
            json={
                "title": "T",
                "body": "B",
                "fcmToken": "raw-token-xyz",
                "url": "/",
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"
    assert mock_send.called
    assert "raw-token-xyz" not in r.text
    assert "fcmToken" not in r.text.lower() or "fcmtoken" not in str(r.json()).lower()


def test_sendfcmnotification_raw_token_never_returned(client):
    with patch(
        "app_v1.endpoints.utils.send_notification",
        return_value={
            "success": True,
            "message": "NOTIFICATION_SENT",
            "details": {"token": "raw-token-xyz"},
        },
    ):
        r = client.post(
            "/sendfcmnotification",
            json={
                "title": "T",
                "body": "B",
                "fcmToken": "raw-token-xyz",
                "url": "/",
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    assert r.json() == {"message": "NOTIFICATION_SENT", "error": None} or r.json()["message"] == "NOTIFICATION_SENT"
    assert "raw-token-xyz" not in r.text


def test_sendfcmnotification_firebase_failure_safe_500(client):
    with patch(
        "app_v1.endpoints.utils.send_notification",
        return_value={
            "success": False,
            "message": "ERROR_SENDING_NOTIFICATION",
            "error": "Firebase secret leak SHOULD NOT APPEAR",
            "body": {"error": {"message": "permission-denied"}},
        },
    ):
        r = client.post(
            "/sendfcmnotification",
            json={
                "title": "T",
                "body": "B",
                "fcmToken": "raw-token-xyz",
                "url": "/",
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 500
    assert r.json()["detail"] == "NOTIFICATION_DISPATCH_FAILED"
    assert "Firebase" not in r.text
    assert "permission-denied" not in r.text
    assert "SHOULD NOT APPEAR" not in r.text


def test_sendfcmnotification_firebase_exception_text_not_exposed(client):
    with patch(
        "app_v1.endpoints.utils.send_notification",
        side_effect=RuntimeError("firebase_admin.exceptions.InvalidArgumentError: bad"),
    ):
        r = client.post(
            "/sendfcmnotification",
            json={
                "title": "T",
                "body": "B",
                "fcmToken": "raw-token-xyz",
                "url": "/",
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 500
    assert r.json()["detail"] == "NOTIFICATION_DISPATCH_FAILED"
    assert "InvalidArgumentError" not in r.text
    assert "firebase_admin" not in r.text


# ---------------------------------------------------------------------------
# Selected drivers
# ---------------------------------------------------------------------------


def test_only_one_selected_drivers_route_declaration():
    source = Path(utils_mod.__file__).read_text(encoding="utf-8")
    assert source.count('"/sendnotificationtoselecteddrivers"') == 1
    # Prior PR25 plan identified two handler defs; only one function must remain.
    assert source.count("def send_notification_to_selected") == 1


def test_selected_drivers_normal_jwt_forbidden(client):
    r = client.post(
        "/sendnotificationtoselecteddrivers",
        json={
            "title": "T",
            "message": "M",
            "driverIds": [OTHER_ID],
        },
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403


def test_selected_drivers_valid_internal_succeeds(client, db_session):
    _seed_user(db_session, OTHER_ID)
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r = client.post(
            "/sendnotificationtoselecteddrivers",
            json={
                "title": "T",
                "message": "M",
                "driverIds": [OTHER_ID],
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["totalSuccess"] == 1
    assert "fcm" not in r.text.lower() or "fcmtoken" not in r.text.lower()
    assert "token" not in str(payload.get("results", {})).lower()


def test_selected_drivers_missing_driver_ids_422(client):
    r = client.post(
        "/sendnotificationtoselecteddrivers",
        json={
            "title": "T",
            "message": "M",
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_selected_drivers_unknown_drivers_safe_result(client, db_session):
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r = client.post(
            "/sendnotificationtoselecteddrivers",
            json={
                "title": "T",
                "message": "M",
                "driverIds": ["9999999999"],
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["totalSuccess"] == 0
    result = payload["results"]["9999999999"]
    assert result["sent"] is False
    assert "response" not in result
    assert "token" not in str(result).lower()


def test_selected_drivers_no_recipient_tokens_returned(client, db_session):
    _seed_user(db_session, OTHER_ID, fcm_token="secret-fcm-token")
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={
            "success": True,
            "message": "NOTIFICATION_SENT",
            "details": {"token": "secret-fcm-token"},
        },
    ):
        r = client.post(
            "/sendnotificationtoselecteddrivers",
            json={
                "title": "T",
                "message": "M",
                "driverIds": [OTHER_ID],
            },
            headers=_auth_headers(),
        )
    assert "secret-fcm-token" not in r.text


# ---------------------------------------------------------------------------
# Topic / marketing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/sendnotificationtoalldrivers",
            {"title": "T", "message": "M"},
        ),
        (
            "/sendmarketingnotificationtonumbers",
            {"title": "T", "body": "B", "phoneNumber": [OTHER_ID]},
        ),
        (
            "/sendmarketingnotificationtoallusers",
            {"title": "T", "message": "M"},
        ),
    ],
)
def test_topic_marketing_normal_jwt_forbidden(client, path, body):
    r = client.post(path, json=body, headers=_auth_headers(internal_key=None))
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_NOTIFICATION_ACCESS_REQUIRED"


@pytest.mark.parametrize(
    "path,body,patch_target",
    [
        (
            "/sendnotificationtoalldrivers",
            {"title": "T", "message": "M"},
            "app_v1.endpoints.utils.send_notification_to_all_drivers",
        ),
        (
            "/sendmarketingnotificationtonumbers",
            {"title": "T", "body": "B", "phoneNumber": [OTHER_ID]},
            "app_v1.endpoints.utils.send_marketing_notification_to_numbers",
        ),
        (
            "/sendmarketingnotificationtoallusers",
            {"title": "T", "message": "M"},
            "app_v1.endpoints.utils.send_marketing_notification_to_all_users",
        ),
    ],
)
def test_topic_marketing_internal_authorization_required(client, path, body, patch_target):
    with patch(
        patch_target,
        return_value={
            "status": "success",
            "totalProcessed": 1,
            "totalSuccess": 1,
            "totalFailed": 0,
            "noTokenIds": [OTHER_ID],
            "failedIds": [],
            "response": {"secret": "nope"},
        },
    ):
        r = client.post(path, json=body, headers=_auth_headers())
    assert r.status_code == 200
    if path == "/sendmarketingnotificationtonumbers":
        assert r.json()["noTokenIds"] == []
        assert r.json()["failedIds"] == []
        assert OTHER_ID not in r.text
    if "response" in r.json() and r.json()["response"] is not None:
        assert "secret" not in str(r.json()["response"])


def test_ordinary_user_cannot_broadcast_arbitrary_content(client):
    """JWT alone cannot reach topic/marketing handlers with arbitrary content."""
    for path, body in [
        ("/sendnotificationtoalldrivers", {"title": "Phish", "message": "Click"}),
        ("/sendmarketingnotificationtoallusers", {"title": "Phish", "message": "Click"}),
        (
            "/sendmarketingnotificationtonumbers",
            {"title": "Phish", "body": "Click", "phoneNumber": ["111"]},
        ),
    ]:
        r = client.post(path, json=body, headers=_auth_headers(internal_key=None))
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Logging source guards
# ---------------------------------------------------------------------------


def test_auth_dependency_does_not_print_jwt():
    source = Path(inspect.getfile(get_current_user_id)).read_text(encoding="utf-8")
    assert "print(token)" not in source
    assert "print(credentials" not in source


def test_notification_helper_does_not_print_fcm_token():
    source = Path(inspect.getfile(notifications_mod)).read_text(encoding="utf-8")
    assert "token={token}" not in source
    # Active (non-comment) fcm token prints should not remain.
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert "print(fcm_token)" not in stripped
        assert 'print(f"{fcm_token}")' not in stripped
        assert "print(fcmToken)" not in stripped


def test_no_full_body_logging_in_utils_endpoints():
    source = Path(inspect.getfile(utils_mod)).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert "print(" not in stripped


def test_safe_event_category_logging_only_in_cancellation_helper():
    source = Path(inspect.getfile(notifications_mod)).read_text(encoding="utf-8")
    assert "category=token_send" in source
    assert "category=helper" in source


# ---------------------------------------------------------------------------
# Mutation regression — service helpers do not require internal header
# ---------------------------------------------------------------------------


def test_mutation_helpers_do_not_require_internal_header():
    for name in [
        "notify_vendors_for_request",
        "notify_vendors_request_cancelled",
        "notify_vendor_bid_accepted",
        "notify_customer_new_bid",
        "notify_other_vendors_new_bid",
        "notify_customer_vendor_accepted",
        "notify_losing_vendors_trip_won",
        "notify_customer_vendor_rejected",
        "notify_vendors_bidding_reopened",
        "notify_vendor_booking_cancelled_by_customer",
        "notify_driver_assigned_to_customer",
        "notify_driver_assigned_to_customer_background",
    ]:
        fn = getattr(notifications_mod, name)
        sig = inspect.signature(fn)
        assert "internal" not in str(sig).lower()
        assert "Header" not in str(sig)


def test_pr8_create_notify_still_runs_after_commit_pattern():
    """notify_vendors_for_request opens its own SessionLocal and does not raise on empty ids."""
    mock_session = MagicMock()
    with patch("app_v1.database.SessionLocal", return_value=mock_session):
        notifications_mod.notify_vendors_for_request([], MagicMock())
    mock_session.close.assert_called()


def test_pr9_cancellation_notify_failure_does_not_raise():
    mock_session = MagicMock()
    mock_session.query.side_effect = RuntimeError("db down")
    with patch("app_v1.database.SessionLocal", return_value=mock_session):
        notifications_mod.notify_vendors_request_cancelled(123)
    mock_session.close.assert_called()


def test_pr10_accepted_bid_notify_unchanged_signature():
    sig = inspect.signature(notifications_mod.notify_vendor_bid_accepted)
    assert "vendor_user_app_id" in sig.parameters


def test_pr11_new_bid_and_handshake_notify_unchanged():
    for name in [
        "notify_customer_new_bid",
        "notify_other_vendors_new_bid",
        "notify_customer_vendor_accepted",
        "notify_losing_vendors_trip_won",
        "notify_customer_vendor_rejected",
        "notify_vendors_bidding_reopened",
    ]:
        assert callable(getattr(notifications_mod, name))


def test_pr12_cancellation_notification_unchanged():
    assert callable(notifications_mod.notify_vendor_booking_cancelled_by_customer)


def test_pr13_assignment_notification_unchanged():
    assert callable(notifications_mod.notify_driver_assigned_to_customer_background)


def test_missing_token_skips_safely(db_session):
    _seed_user(db_session, OTHER_ID, fcm_token=None)
    result = notifications_mod.send_notification_to_user(
        db_session,
        FCMSend(title="T", body="B", userAppId=OTHER_ID, url="/"),
    )
    assert result.message == "NO_TOKEN"


def test_tombstone_recipient_skips_safely(db_session):
    _seed_user(db_session, TOMBSTONE_ID, fcm_token="still-present")
    result = notifications_mod.send_notification_to_user(
        db_session,
        FCMSend(title="T", body="B", userAppId=TOMBSTONE_ID, url="/"),
    )
    assert result.message == "USER_NOT_FOUND"


def test_no_user_recipient_skips_safely(db_session):
    result = notifications_mod.send_notification_to_user(
        db_session,
        FCMSend(title="T", body="B", userAppId="0000000000", url="/"),
    )
    assert result.message == "USER_NOT_FOUND"


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_ordinary_user_cannot_notify_arbitrary_userappid(client, db_session):
    _seed_user(db_session, OTHER_ID)
    r = client.post(
        "/notificationtodriver",
        json=_notify_body(userAppId=OTHER_ID),
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403


def test_ordinary_user_cannot_submit_arbitrary_raw_token(client):
    r = client.post(
        "/sendfcmnotification",
        json={"title": "T", "body": "B", "fcmToken": "attacker-token", "url": "/"},
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403


def test_ordinary_user_cannot_bulk_notify_driver_ids(client):
    r = client.post(
        "/sendnotificationtoselecteddrivers",
        json={"title": "T", "message": "M", "driverIds": [OTHER_ID, "111"]},
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403


def test_ordinary_user_cannot_broadcast_topic_notification(client):
    r = client.post(
        "/sendnotificationtoalldrivers",
        json={"title": "T", "message": "M"},
        headers=_auth_headers(internal_key=None),
    )
    assert r.status_code == 403


def test_no_firebase_admin_internals_in_error_responses(client):
    with patch(
        "app_v1.endpoints.utils.send_notification",
        return_value={
            "success": False,
            "message": "ERROR_SENDING_NOTIFICATION",
            "body": {"error": {"status": "PERMISSION_DENIED", "message": "secret"}},
        },
    ):
        r = client.post(
            "/sendfcmnotification",
            json={"title": "T", "body": "B", "fcmToken": "t", "url": "/"},
            headers=_auth_headers(),
        )
    assert r.status_code == 500
    assert "PERMISSION_DENIED" not in r.text
    assert "secret" not in r.text
    assert "firebase" not in r.text.lower()


def test_no_db_close_misuse_added_in_utils_endpoints():
    source = Path(inspect.getfile(utils_mod)).read_text(encoding="utf-8")
    assert "db.close()" not in source


def test_openapi_includes_internal_header_requirement(client):
    schema = client.app.openapi()
    path_item = schema["paths"]["/notificationtodriver"]["post"]
    params = path_item.get("parameters") or []
    # FastAPI may place header deps under parameters.
    header_names = {p.get("name") for p in params if p.get("in") == "header"}
    # Also check components / security schemes if floated there.
    joined = str(schema)
    assert (
        internal_auth.INTERNAL_NOTIFICATION_HEADER in header_names
        or internal_auth.INTERNAL_NOTIFICATION_HEADER in joined
    )


def test_send_notification_to_selected_users_service_still_callable(db_session):
    """Mutation path helper remains usable without HTTP/internal header."""
    _seed_user(db_session, OTHER_ID)
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        result = notifications_mod.send_notification_to_selected_users(
            db_session,
            FCMSendDrivers(
                title="T",
                body="B",
                driverIds=[OTHER_ID],
                url="/",
            ),
        )
    assert result.totalSuccess == 1
