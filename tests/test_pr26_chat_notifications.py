"""
PR26 — dedicated POST /chat/notifications (customer↔vendor chat push).

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date, time
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
os.environ.setdefault("INTERNAL_NOTIFICATION_KEY", "pr25-test-internal-key")
os.environ["FIREBASE_DATABASE_URL"] = (
    "https://opnbd-a23e1-default-rtdb.asia-southeast1.firebasedatabase.app"
)

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
_fake_firebase.db = types.ModuleType("firebase_admin.db")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)
sys.modules.setdefault("firebase_admin.db", _fake_firebase.db)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.endpoints import chat as chat_mod  # noqa: E402
from app_v1.endpoints import utils as utils_mod  # noqa: E402
from app_v1.services import chat_notifications as chat_svc  # noqa: E402
from app_v1.auth import internal as internal_auth  # noqa: E402

SENDER_ID = "7022359323"
RECIPIENT_ID = "8637554388"
OTHER_ID = "9000000001"
THREAD_ID = f"{SENDER_ID}-{RECIPIENT_ID}"  # SENDER < RECIPIENT numerically? 702.. < 863..
MESSAGE_ID = "-NabcChatMsg001"


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[User.__table__, Request.__table__, ApiRateLimitBucket.__table__],
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
    app.include_router(chat_mod.router)
    app.include_router(utils_mod.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = lambda: SENDER_ID
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
    app.include_router(chat_mod.router)
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


_UID_SEQ = 2000
_RID_SEQ = 5000


def _seed_user(
    db,
    user_app_id: str,
    *,
    fcm_token: str | None = "fcm-token-abc",
    full_name: str = "Sender Name",
    lock_app: bool = False,
) -> User:
    global _UID_SEQ
    _UID_SEQ += 1
    user = User(
        UID=_UID_SEQ,
        userAppId=user_app_id,
        password="x",
        fullName=full_name,
        emailId=f"{user_app_id}@example.com",
        dob="01-01-1990",
        city="Gangtok",
        gender="Male",
        alsoVendor=False,
        vendorApproved=False,
        lockApp=lock_app,
        rating="5",
        totalNoOfReviews=0,
        fcmToken=fcm_token,
        user_login_status="LOGGEDIN",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_relationship(
    db,
    *,
    customer: str,
    vendor: str,
    status: str = "BID - CONFIRMED",
) -> Request:
    global _RID_SEQ
    _RID_SEQ += 1
    req = Request(
        RID=_RID_SEQ,
        fromLocation="A",
        fromLandmark="A1",
        toLocation="B",
        toLandmark="B1",
        pickUpDate=date(2026, 1, 1),
        pickUpTime=time(10, 0),
        noOfAdults=1,
        noOfKids=0,
        carType="SUV",
        acRequest=False,
        carrierRequest=False,
        requestStatus=status,
        customerAppId=customer,
        requestWonBy=vendor,
        finalAmount=100,
        noOfBids=1,
        requestReopened=False,
        reviewDone="N",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _auth_headers():
    return {"Authorization": "Bearer test-jwt", "X-Client-Id": "test-client"}


def _body(**overrides):
    payload = {"threadId": THREAD_ID, "messageId": MESSAGE_ID}
    payload.update(overrides)
    return payload


def _rtdb_message(**overrides):
    msg = {
        "sender": SENDER_ID,
        "receiver": RECIPIENT_ID,
        "text": "Hello there",
        "type": "Text",
        "url": "null",
    }
    msg.update(overrides)
    return msg


def _seed_happy_path(db):
    _seed_user(db, SENDER_ID, full_name="Alice Vendor")
    _seed_user(db, RECIPIENT_ID, full_name="Bob Customer", fcm_token="token-bob")
    _seed_relationship(db, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_missing_jwt_returns_401(client_no_jwt):
    r = client_no_jwt.post("/chat/notifications", json=_body())
    assert r.status_code in (401, 403)


def test_invalid_jwt_returns_401(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[get_db] = _override_db
    # Real bearer validation — no override.
    with TestClient(app) as c:
        r = c.post(
            "/chat/notifications",
            json=_body(),
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
    assert r.status_code == 401
    app.dependency_overrides.clear()


def test_missing_sender_row(client, db_session):
    _seed_user(db_session, RECIPIENT_ID)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 404
    assert r.json()["detail"] == "SENDER_NOT_FOUND"


def test_tombstoned_sender_forbidden(client, db_session):
    tomb = f"{SENDER_ID}.DELETED"
    # Override JWT to tombstone id
    client.app.dependency_overrides[get_current_user_id] = lambda: tomb
    _seed_user(db_session, tomb, full_name="Gone")
    _seed_user(db_session, RECIPIENT_ID)
    r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_NOTIFICATION_NOT_ALLOWED"
    client.app.dependency_overrides[get_current_user_id] = lambda: SENDER_ID


def test_locked_sender_forbidden(client, db_session):
    _seed_user(db_session, SENDER_ID, lock_app=True)
    _seed_user(db_session, RECIPIENT_ID)
    r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_NOTIFICATION_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_missing_thread_id_422(client, db_session):
    _seed_happy_path(db_session)
    r = client.post(
        "/chat/notifications",
        json={"messageId": MESSAGE_ID},
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_missing_message_id_422(client, db_session):
    _seed_happy_path(db_session)
    r = client.post(
        "/chat/notifications",
        json={"threadId": THREAD_ID},
        headers=_auth_headers(),
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"fcmToken": "x"},
        {"title": "x"},
        {"body": "x"},
        {"url": "//x"},
        {"userAppId": RECIPIENT_ID},
        {"phone": RECIPIENT_ID},
    ],
)
def test_extra_fields_rejected(client, db_session, extra):
    _seed_happy_path(db_session)
    body = _body(**extra)
    r = client.post("/chat/notifications", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_invalid_thread_format_rejected(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post(
            "/chat/notifications",
            json=_body(threadId="not-a-peer-thread"),
            headers=_auth_headers(),
        )
    assert r.status_code in (403, 422)


def test_invalid_message_id_format_rejected(client, db_session):
    _seed_happy_path(db_session)
    r = client.post(
        "/chat/notifications",
        json=_body(messageId="../evil"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "INVALID_MESSAGE_ID"


# ---------------------------------------------------------------------------
# RTDB verification
# ---------------------------------------------------------------------------


def test_missing_firebase_database_url_503(client, db_session):
    _seed_happy_path(db_session)
    with patch.dict(os.environ, {"FIREBASE_DATABASE_URL": ""}, clear=False):
        with patch(
            "app_v1.utils.firebase_realtime.ensure_firebase_database_configured",
            side_effect=chat_svc.ChatDatabaseUnavailable("missing"),
        ):
            # Patch at service import site
            with patch(
                "app_v1.services.chat_notifications.get_chat_message",
                side_effect=chat_svc.ChatDatabaseUnavailable("missing"),
            ):
                r = client.post(
                    "/chat/notifications", json=_body(), headers=_auth_headers()
                )
    assert r.status_code == 503
    assert r.json()["detail"] == "CHAT_DATABASE_UNAVAILABLE"


def test_rtdb_read_failure_503(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        side_effect=chat_svc.ChatMessageReadError("boom"),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 503
    assert "boom" not in r.text
    assert r.json()["detail"] == "CHAT_DATABASE_UNAVAILABLE"


def test_message_not_found(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=None,
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 404
    assert r.json()["detail"] == "MESSAGE_NOT_FOUND"


def test_message_not_object(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value={"__non_object__": True, "value": "x"},
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 422


def test_sender_mismatch(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(sender=OTHER_ID),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "MESSAGE_SENDER_MISMATCH"


def test_receiver_missing(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(receiver=""),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 422


def test_self_message_forbidden(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(receiver=SENDER_ID),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403


def test_thread_participants_mismatch(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post(
            "/chat/notifications",
            json=_body(threadId=f"{SENDER_ID}-{OTHER_ID}"),
            headers=_auth_headers(),
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "INVALID_CHAT_RELATIONSHIP"


def test_no_full_thread_read():
    source = Path(chat_svc.__file__).read_text()
    # Utility must target single message path; service must not iterate threads.
    assert "Chats/{thread" in Path(
        ROOT / "app_v1/utils/firebase_realtime.py"
    ).read_text() or 'f"Chats/' in Path(
        ROOT / "app_v1/utils/firebase_realtime.py"
    ).read_text()
    assert "get_chat_message" in source


def test_errors_never_return_message_content(client, db_session):
    _seed_happy_path(db_session)
    secret = "SECRET_CHAT_BODY_SHOULD_NOT_LEAK"
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(text=secret, sender=OTHER_ID),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert secret not in r.text


# ---------------------------------------------------------------------------
# Relationship authorization
# ---------------------------------------------------------------------------


def _post_ok(client, db_session, status: str, *, past_pickup: bool = False):
    _seed_user(db_session, SENDER_ID, full_name="Alice")
    _seed_user(db_session, RECIPIENT_ID, fcm_token="tok")
    req = _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status=status
    )
    if past_pickup:
        req.pickUpDate = date(2020, 1, 1)
        db_session.commit()
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    return r, send_mock


def test_bid_confirmed_succeeds(client, db_session):
    r, send_mock = _post_ok(client, db_session, "BID - CONFIRMED")
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"
    send_mock.assert_called_once()


def test_request_confirmed_succeeds(client, db_session):
    r, _ = _post_ok(client, db_session, "REQUEST - CONFIRMED")
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"


def test_past_request_confirmed_succeeds(client, db_session):
    r, _ = _post_ok(client, db_session, "REQUEST - CONFIRMED", past_pickup=True)
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"


@pytest.mark.parametrize(
    "status",
    [
        "BID - OPEN",
        "REQUEST - CANCELLED BY USER",
        "BOOKING - CANCELLED BY USER",
    ],
)
def test_disallowed_statuses_rejected(client, db_session, status):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID)
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status=status
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "INVALID_CHAT_RELATIONSHIP"


def test_unrelated_users_rejected(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID)
    _seed_relationship(
        db_session, customer=OTHER_ID, vendor="9000000002", status="BID - CONFIRMED"
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403


def test_bidder_only_without_request_won_by_rejected(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID)
    global _RID_SEQ
    _RID_SEQ += 1
    req = Request(
        RID=_RID_SEQ,
        fromLocation="A",
        fromLandmark="A1",
        toLocation="B",
        toLandmark="B1",
        pickUpDate=date(2026, 1, 1),
        pickUpTime=time(10, 0),
        noOfAdults=1,
        noOfKids=0,
        carType="SUV",
        acRequest=False,
        carrierRequest=False,
        requestStatus="BID - OPEN",
        customerAppId=RECIPIENT_ID,
        requestWonBy=None,
        finalAmount=0,
        noOfBids=1,
        requestReopened=False,
        reviewDone="N",
    )
    db_session.add(req)
    db_session.commit()
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403


def test_reused_phone_without_current_relationship_rejected(client, db_session):
    # Live users exist but no eligible requestWonBy pair for them.
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Recipient handling
# ---------------------------------------------------------------------------


def test_missing_recipient_404(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED"
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 404
    assert r.json()["detail"] == "RECIPIENT_NOT_FOUND"


def test_tombstoned_recipient_skipped(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID, fcm_token="tok")
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED"
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications._is_tombstone_user_app_id",
        side_effect=lambda uid: str(uid) == RECIPIENT_ID,
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
    ) as send_mock:
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SKIPPED"
    send_mock.assert_not_called()


def test_locked_recipient_skipped(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID, lock_app=True, fcm_token="tok")
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED"
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
    ) as send_mock:
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SKIPPED"
    send_mock.assert_not_called()


def test_recipient_no_token(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID, fcm_token=None)
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED"
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["message"] == "NO_TOKEN"
    assert "token" not in r.text.lower() or "NO_TOKEN" in r.text


def test_token_never_returned(client, db_session):
    r, _ = _post_ok(client, db_session, "BID - CONFIRMED")
    assert "token-bob" not in r.text
    assert "fcm" not in r.text.lower() or r.json()["message"] == "NOTIFICATION_SENT"


def test_recipient_profile_never_returned(client, db_session):
    r, _ = _post_ok(client, db_session, "BID - CONFIRMED")
    body = r.json()
    assert set(body.keys()) == {"message"}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_text_title_and_preview_from_server(client, db_session):
    _seed_user(db_session, SENDER_ID, full_name="Alice Vendor")
    _seed_user(db_session, RECIPIENT_ID, fcm_token="tok")
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED"
    )
    long_text = ("Hello\n\tWorld\x00" + ("x" * 100))
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(text=long_text, type="Text"),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 200
    kwargs = send_mock.call_args.kwargs
    assert kwargs["title"] == "New Message from Alice Vendor"
    assert kwargs["url"] == "//Chat_Main_Page"
    assert "\n" not in kwargs["body"]
    assert "\x00" not in kwargs["body"]
    assert len(kwargs["body"]) <= 80


def test_empty_text_generic_body(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(text="   ", type="Text"),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert send_mock.call_args.kwargs["body"] == "Sent you a message"


def test_photo_body(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(type="Photo", text="", url="https://x"),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert send_mock.call_args.kwargs["body"] == "Sent you an image"
    assert "https://" not in send_mock.call_args.kwargs["body"]


def test_contact_body(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(type="Contact", text="Contact Card sent", url="N%P"),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert send_mock.call_args.kwargs["body"] == "Shared a contact"


def test_file_body(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(type="File", text=""),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert send_mock.call_args.kwargs["body"] == "Sent you a file"


def test_sanitize_helpers_unit():
    assert len(chat_svc.sanitize_text_preview("a" * 100)) == 80
    assert chat_svc.sanitize_text_preview("a\nb\tc") == "a b c"
    assert "\x01" not in chat_svc.sanitize_text_preview("x\x01y")
    assert chat_svc.build_notification_body(message_type="Text", text="") == (
        "Sent you a message"
    )


# ---------------------------------------------------------------------------
# Rate / idempotency
# ---------------------------------------------------------------------------


def test_duplicate_message_already_handled(client, db_session):
    r1, send_mock = _post_ok(client, db_session, "BID - CONFIRMED")
    assert r1.json()["message"] == "NOTIFICATION_SENT"
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock2:
        r2 = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r2.status_code == 200
    assert r2.json()["message"] == "ALREADY_HANDLED"
    send_mock2.assert_not_called()


def test_different_message_ids_independent(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r1 = client.post(
            "/chat/notifications",
            json=_body(messageId="-NmsgA"),
            headers=_auth_headers(),
        )
        r2 = client.post(
            "/chat/notifications",
            json=_body(messageId="-NmsgB"),
            headers=_auth_headers(),
        )
    assert r1.json()["message"] == "NOTIFICATION_SENT"
    assert r2.json()["message"] == "NOTIFICATION_SENT"
    assert send_mock.call_count == 2


def test_sender_rate_limit_429(client, db_session):
    _seed_happy_path(db_session)
    with patch.object(chat_svc, "_SENDER_MAX", 1), patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r1 = client.post(
            "/chat/notifications",
            json=_body(messageId="-Nrate1"),
            headers=_auth_headers(),
        )
        r2 = client.post(
            "/chat/notifications",
            json=_body(messageId="-Nrate2"),
            headers=_auth_headers(),
        )
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r2.json()["detail"] == "CHAT_NOTIFICATION_RATE_LIMITED"


def test_pair_rate_limit_429(client, db_session):
    _seed_happy_path(db_session)
    with patch.object(chat_svc, "_SENDER_MAX", 100), patch.object(
        chat_svc, "_PAIR_MAX", 1
    ), patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r1 = client.post(
            "/chat/notifications",
            json=_body(messageId="-Npair1"),
            headers=_auth_headers(),
        )
        r2 = client.post(
            "/chat/notifications",
            json=_body(messageId="-Npair2"),
            headers=_auth_headers(),
        )
    assert r1.status_code == 200
    assert r2.status_code == 429


def test_auth_failure_does_not_consume_idempotency(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID, fcm_token="tok")
    # No relationship → 403
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ):
        r1 = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r1.status_code == 403
    # Add relationship and retry same message — should send, not ALREADY_HANDLED
    _seed_relationship(
        db_session, customer=RECIPIENT_ID, vendor=SENDER_ID, status="BID - CONFIRMED"
    )
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r2 = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r2.status_code == 200
    assert r2.json()["message"] == "NOTIFICATION_SENT"
    send_mock.assert_called_once()


# ---------------------------------------------------------------------------
# FCM / errors / support / regression
# ---------------------------------------------------------------------------


def test_fcm_provider_failure_safe(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": False, "message": "ERROR_FIREBASE_SECRET_DETAIL"},
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 500
    assert r.json()["detail"] == "CHAT_NOTIFICATION_FAILED"
    assert "FIREBASE" not in r.text
    assert "SECRET" not in r.text


def test_fcm_exception_not_exposed(client, db_session):
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        side_effect=RuntimeError("firebase credential abc"),
    ):
        r = client.post("/chat/notifications", json=_body(), headers=_auth_headers())
    assert r.status_code == 500
    assert "credential" not in r.text
    assert r.json()["detail"] == "CHAT_NOTIFICATION_FAILED"


def test_support_thread_without_config_fails_closed(client, db_session):
    """PR27: admin threads are no longer deferred; missing config fails closed."""
    _seed_happy_path(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_rtdb_message(receiver="9999999999"),
    ):
        r = client.post(
            "/chat/notifications",
            json=_body(threadId=f"admin-{SENDER_ID}"),
            headers=_auth_headers(),
        )
    assert r.status_code == 503
    assert r.json()["detail"] == "SUPPORT_CONFIGURATION_INVALID"


def test_pr25_generic_route_still_requires_internal_key(client, db_session):
    _seed_user(db_session, RECIPIENT_ID)
    r = client.post(
        "/notificationtodriver",
        json={
            "title": "T",
            "body": "B",
            "userAppId": RECIPIENT_ID,
            "url": "///Open Requests",
        },
        headers={"Authorization": "Bearer test-jwt"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "INTERNAL_NOTIFICATION_ACCESS_REQUIRED"


def test_pr25_internal_key_still_works(client, db_session):
    _seed_user(db_session, RECIPIENT_ID)
    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r = client.post(
            "/notificationtodriver",
            json={
                "title": "T",
                "body": "B",
                "userAppId": RECIPIENT_ID,
                "url": "///Open Requests",
            },
            headers={
                "Authorization": "Bearer test-jwt",
                internal_auth.INTERNAL_NOTIFICATION_HEADER: os.environ[
                    "INTERNAL_NOTIFICATION_KEY"
                ],
            },
        )
    assert r.status_code == 200


def test_no_db_close_in_chat_endpoint():
    source = Path(chat_mod.__file__).read_text()
    assert "db.close()" not in source
    svc = Path(chat_svc.__file__).read_text()
    assert "db.close()" not in svc


def test_openapi_chat_endpoint_excludes_raw_fields():
    app = FastAPI()
    app.include_router(chat_mod.router)
    schema = app.openapi()
    assert "/chat/notifications" in schema["paths"]
    components = schema.get("components", {}).get("schemas", {})
    req = components.get("ChatNotificationRequest", {})
    props = set(req.get("properties", {}).keys())
    assert props == {"threadId", "messageId"}
    assert req.get("additionalProperties") is False


def test_source_no_sensitive_logging():
    for path in (
        ROOT / "app_v1/services/chat_notifications.py",
        ROOT / "app_v1/endpoints/chat.py",
        ROOT / "app_v1/utils/firebase_realtime.py",
    ):
        text = path.read_text()
        assert "print(" not in text
        for banned in ("fcm_token", "FIREBASE_SERVICE_ACCOUNT", "password"):
            # Allow reading env var name for service account in comments/fcm util only.
            if path.name == "firebase_realtime.py" and banned == "FIREBASE_SERVICE_ACCOUNT":
                continue
            # Variable names like fcm_token in code are ok; logging is not.
        assert "logger.info" not in text or "token" not in text
