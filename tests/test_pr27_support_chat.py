"""
PR27 — support chat config + support/admin POST /chat/notifications.

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
from app_v1.models.admin_number import AdminNumber  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.endpoints import chat as chat_mod  # noqa: E402
from app_v1.endpoints import utils as utils_mod  # noqa: E402
from app_v1.services import chat_notifications as chat_svc  # noqa: E402
from app_v1.auth import internal as internal_auth  # noqa: E402

USER_ID = "7022359323"
SUPPORT_ID = "9999000001"
OTHER_USER = "8637554388"
PEER_VENDOR = "7022359323"
PEER_CUSTOMER = "8637554388"
MESSAGE_ID = "-NabcSupport001"


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
            Request.__table__,
            ApiRateLimitBucket.__table__,
            AdminNumber.__table__,
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
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
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


def _make_client(engine, jwt_sub: str):
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
    app.dependency_overrides[get_current_user_id] = lambda: jwt_sub
    return TestClient(app), app


_UID_SEQ = 3000
_RID_SEQ = 7000
_ADMIN_SEQ = 100


def _seed_user(
    db,
    user_app_id: str,
    *,
    fcm_token: str | None = "fcm-token-abc",
    full_name: str = "Test User",
    lock_app: bool = False,
    email: str = None,
    profile_picture: str = None,
) -> User:
    global _UID_SEQ
    _UID_SEQ += 1
    user = User(
        UID=_UID_SEQ,
        userAppId=user_app_id,
        password="x",
        fullName=full_name,
        emailId=email or f"{user_app_id}@example.com",
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
        profilePicture=profile_picture,
        bankAccountNo="1234567890",
        imageAadhar="secret-aadhar",
        imagePAN="secret-pan",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_admin(db, phone: str) -> AdminNumber:
    global _ADMIN_SEQ
    _ADMIN_SEQ += 1
    row = AdminNumber(id=_ADMIN_SEQ, phonenumber=phone)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_support_config(db, *, support_fcm: str | None = "fcm-support"):
    _seed_user(db, SUPPORT_ID, full_name="OpenBid Support Op", fcm_token=support_fcm)
    _seed_admin(db, SUPPORT_ID)


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


def _support_rtdb(**overrides):
    msg = {
        "sender": USER_ID,
        "receiver": SUPPORT_ID,
        "text": "Help me please with secret details",
        "type": "Text",
        "url": "https://example.com/secret-media.jpg",
    }
    msg.update(overrides)
    return msg


def _notify(client, *, thread_id: str, message_id: str = MESSAGE_ID):
    return client.post(
        "/chat/notifications",
        json={"threadId": thread_id, "messageId": message_id},
        headers=_auth_headers(),
    )


# ---------------------------------------------------------------------------
# Support configuration GET /chat/support/config
# ---------------------------------------------------------------------------


def test_01_config_missing_jwt_401(client_no_jwt):
    r = client_no_jwt.get("/chat/support/config")
    assert r.status_code in (401, 403)


def test_02_config_invalid_jwt_401(engine):
    app = FastAPI()
    app.include_router(chat_mod.router)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        r = c.get(
            "/chat/support/config",
            headers={"Authorization": "Bearer not-a-real-jwt", "X-Client-Id": "c"},
        )
    assert r.status_code in (401, 403)


def test_03_zero_admin_rows_unavailable(client, db_session):
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["supportUserAppId"] is None
    assert body["displayName"] == "OpenBid Support"
    assert body["profileImageUrl"] is None


def test_04_multiple_admin_rows_unavailable(client, db_session):
    _seed_user(db_session, SUPPORT_ID)
    _seed_admin(db_session, SUPPORT_ID)
    _seed_admin(db_session, "8888000002")
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_05_exactly_one_valid_available(client, db_session):
    _seed_support_config(db_session, support_fcm="tok")
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["supportUserAppId"] == SUPPORT_ID
    assert body["displayName"] == "OpenBid Support"


def test_06_config_missing_user_unavailable(client, db_session):
    _seed_admin(db_session, SUPPORT_ID)
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_07_config_tombstoned_user_unavailable(client, db_session):
    _seed_user(db_session, f"{SUPPORT_ID}.DELETED")
    _seed_admin(db_session, f"{SUPPORT_ID}.DELETED")
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_08_config_locked_user_unavailable(client, db_session):
    _seed_user(db_session, SUPPORT_ID, lock_app=True)
    _seed_admin(db_session, SUPPORT_ID)
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_09_support_without_fcm_still_available(client, db_session):
    _seed_support_config(db_session, support_fcm=None)
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["available"] is True


def test_10_13_config_response_privacy(client, db_session):
    _seed_support_config(db_session)
    r = client.get("/chat/support/config", headers=_auth_headers())
    text = r.text.lower()
    body = r.json()
    assert "fcmtoken" not in text
    assert "fcm" not in text
    assert "email" not in text
    assert "bank" not in text
    assert "aadhar" not in text
    assert "pan" not in body
    assert "dob" not in text
    assert "gender" not in text
    assert set(body.keys()) == {
        "available",
        "supportUserAppId",
        "displayName",
        "profileImageUrl",
    }


def test_14_no_client_userappid_query(client, db_session):
    _seed_support_config(db_session)
    r = client.get(
        "/chat/support/config",
        params={"userAppId": OTHER_USER},
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    assert r.json()["supportUserAppId"] == SUPPORT_ID


def test_15_no_sql_leak_on_config(client, db_session):
    r = client.get("/chat/support/config", headers=_auth_headers())
    assert "sqlalchemy" not in r.text.lower()
    assert "traceback" not in r.text.lower()


# ---------------------------------------------------------------------------
# User → support
# ---------------------------------------------------------------------------


def test_16_valid_user_to_support_text(client, db_session, engine):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r = _notify(client, thread_id=f"admin-{USER_ID}")
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"
    kwargs = send_mock.call_args.kwargs
    assert kwargs["title"] == "New Support Message"
    assert kwargs["body"] == "Sent you a message"
    assert kwargs["url"] == "//Chat_Main_Page"
    assert "Help me" not in kwargs["body"]


def test_17_20_templates_and_sender_mismatch(client, db_session, engine):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)

    for msg_type, expected_body in (
        ("Photo", "Sent you an image"),
        ("Contact", "Shared a contact"),
        ("File", "Sent you a file"),
        ("WeirdType", "Sent you a message"),
    ):
        mid = f"-N{msg_type}"
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(type=msg_type, text="SECRET", url="Name%Phone"),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
            return_value={"success": True, "message": "NOTIFICATION_SENT"},
        ) as send_mock:
            r = _notify(client, thread_id=f"admin-{USER_ID}", message_id=mid)
        assert r.status_code == 200, msg_type
        assert send_mock.call_args.kwargs["body"] == expected_body
        assert "SECRET" not in send_mock.call_args.kwargs["body"]
        assert "Name%Phone" not in send_mock.call_args.kwargs["body"]

    # JWT sender mismatch
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(sender=OTHER_USER),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nmismatch")
    assert r.status_code == 403
    assert r.json()["detail"] == "MESSAGE_SENDER_MISMATCH"


def test_21_forged_admin_other_user(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ):
        r = _notify(client, thread_id=f"admin-{OTHER_USER}")
    assert r.status_code == 403
    assert r.json()["detail"] == "INVALID_SUPPORT_CHAT"


def test_22_receiver_not_configured_support(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(receiver=OTHER_USER),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}")
    assert r.status_code == 403
    assert r.json()["detail"] == "INVALID_SUPPORT_CHAT"


def test_23_24_normal_user_cannot_act_as_support(engine, db_session):
    _seed_user(db_session, USER_ID)
    _seed_user(db_session, OTHER_USER)
    _seed_support_config(db_session)
    c, app = _make_client(engine, USER_ID)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(
                sender=USER_ID,
                receiver=OTHER_USER,
            ),
        ):
            r = _notify(c, thread_id=f"admin-{OTHER_USER}")
        assert r.status_code == 403
        assert r.json()["detail"] == "INVALID_SUPPORT_CHAT"

        # Support JWT using user→support self shape
        app.dependency_overrides[get_current_user_id] = lambda: SUPPORT_ID
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(
                sender=SUPPORT_ID,
                receiver=SUPPORT_ID,
            ),
        ):
            r2 = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{SUPPORT_ID}", "messageId": "-Nself"},
                headers=_auth_headers(),
            )
        assert r2.status_code == 403
    app.dependency_overrides.clear()


def test_25_26_locked_tombstoned_sender(client, db_session):
    _seed_support_config(db_session)
    _seed_user(db_session, USER_ID, lock_app=True)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}")
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_NOTIFICATION_NOT_ALLOWED"

    # Replace with tombstoned sender id as JWT — use separate user id
    tombstone = f"{USER_ID}.DELETED"
    _seed_user(db_session, tombstone)
    c, app = _make_client(db_session.get_bind(), tombstone)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=tombstone),
        ):
            r2 = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{tombstone}", "messageId": "-Ntomb"},
                headers=_auth_headers(),
            )
        # tombstone id contains non-digit suffix → invalid thread OR not allowed
        assert r2.status_code in (403, 422)
    app.dependency_overrides.clear()


def test_27_28_missing_and_ambiguous_config(client, db_session):
    _seed_user(db_session, USER_ID)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}")
    assert r.status_code == 503
    assert r.json()["detail"] == "SUPPORT_CONFIGURATION_INVALID"

    _seed_user(db_session, SUPPORT_ID)
    _seed_admin(db_session, SUPPORT_ID)
    _seed_admin(db_session, "8888000002")
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ):
        r2 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Namb")
    assert r2.status_code == 503
    assert r2.json()["detail"] == "SUPPORT_CONFIGURATION_INVALID"


def test_29_support_no_fcm_returns_no_token(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session, support_fcm=None)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
    ) as send_mock:
        r = _notify(client, thread_id=f"admin-{USER_ID}")
    assert r.status_code == 200
    assert r.json()["message"] == "NO_TOKEN"
    send_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Support → user
# ---------------------------------------------------------------------------


def test_31_configured_support_reply_sends(engine, db_session):
    _seed_user(db_session, USER_ID, fcm_token="user-tok")
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(
                sender=SUPPORT_ID,
                receiver=USER_ID,
                text="We can help with SECRET",
            ),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
            return_value={"success": True, "message": "NOTIFICATION_SENT"},
        ) as send_mock:
            r = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{USER_ID}", "messageId": "-Nreply"},
                headers=_auth_headers(),
            )
        assert r.status_code == 200
        assert r.json()["message"] == "NOTIFICATION_SENT"
        assert send_mock.call_args.kwargs["title"] == "OpenBid Support"
        assert send_mock.call_args.kwargs["body"] == "Sent you a message"
        assert "SECRET" not in send_mock.call_args.kwargs["body"]
    app.dependency_overrides.clear()


def test_32_33_ordinary_and_wrong_support_rejected(engine, db_session):
    _seed_user(db_session, USER_ID)
    _seed_user(db_session, OTHER_USER)
    _seed_support_config(db_session)
    c, app = _make_client(engine, USER_ID)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=USER_ID, receiver=OTHER_USER),
        ):
            r = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{OTHER_USER}", "messageId": "-Nfake"},
                headers=_auth_headers(),
            )
        assert r.status_code == 403

        app.dependency_overrides[get_current_user_id] = lambda: OTHER_USER
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=OTHER_USER, receiver=USER_ID),
        ):
            r2 = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{USER_ID}", "messageId": "-Nwrong"},
                headers=_auth_headers(),
            )
        assert r2.status_code == 403
        assert r2.json()["detail"] == "INVALID_SUPPORT_CHAT"
    app.dependency_overrides.clear()


def test_34_36_thread_suffix_and_self_notify(engine, db_session):
    _seed_user(db_session, USER_ID)
    _seed_user(db_session, OTHER_USER)
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c:
        # Thread suffix must equal receiver
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=USER_ID),
        ):
            r = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{OTHER_USER}", "messageId": "-Nsuf"},
                headers=_auth_headers(),
            )
        assert r.status_code == 403

        # Mismatched arbitrary target
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=OTHER_USER),
        ):
            r2 = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{USER_ID}", "messageId": "-Narb"},
                headers=_auth_headers(),
            )
        assert r2.status_code == 403

        # Self-notify
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=SUPPORT_ID),
        ):
            r3 = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{SUPPORT_ID}", "messageId": "-Nslf"},
                headers=_auth_headers(),
            )
        assert r3.status_code == 403
    app.dependency_overrides.clear()


def test_37_40_recipient_outcomes(engine, db_session):
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver="1111111111"),
        ):
            r = c.post(
                "/chat/notifications",
                json={"threadId": "admin-1111111111", "messageId": "-Nmiss"},
                headers=_auth_headers(),
            )
        assert r.status_code == 404
        assert r.json()["detail"] == "RECIPIENT_NOT_FOUND"

        _seed_user(db_session, "2222222222", lock_app=False)
        # tombstone userAppId
        _seed_user(db_session, "3333333333.DELETED")
        # For tombstone: look up by exact id in message
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(
                sender=SUPPORT_ID, receiver="3333333333.DELETED"
            ),
        ):
            r_tomb = c.post(
                "/chat/notifications",
                json={
                    "threadId": "admin-3333333333.DELETED",
                    "messageId": "-NtombR",
                },
                headers=_auth_headers(),
            )
        # malformed admin thread (non-digit suffix) → 422, or if parsed differently 403
        assert r_tomb.status_code in (403, 422)

        locked_id = "4444444444"
        _seed_user(db_session, locked_id, lock_app=True)
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=locked_id),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
        ) as send_mock:
            r_lock = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{locked_id}", "messageId": "-Nlock"},
                headers=_auth_headers(),
            )
        assert r_lock.status_code == 200
        assert r_lock.json()["message"] == "NOTIFICATION_SKIPPED"
        send_mock.assert_not_called()

        no_tok = "5555555555"
        _seed_user(db_session, no_tok, fcm_token=None)
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=no_tok),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
        ) as send_mock2:
            r_nt = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{no_tok}", "messageId": "-Nnotok"},
                headers=_auth_headers(),
            )
        assert r_nt.status_code == 200
        assert r_nt.json()["message"] == "NO_TOKEN"
        send_mock2.assert_not_called()
    app.dependency_overrides.clear()


def test_38_tombstoned_recipient_skipped_via_flag(engine, db_session):
    """Recipient row exists; tombstone detection → NOTIFICATION_SKIPPED."""
    _seed_user(db_session, USER_ID, fcm_token="t")
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=USER_ID),
        ), patch(
            "app_v1.services.chat_notifications._is_tombstone_user_app_id",
            side_effect=lambda uid: str(uid) == USER_ID,
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
        ) as send_mock:
            r = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{USER_ID}", "messageId": "-Ntskip"},
                headers=_auth_headers(),
            )
        assert r.status_code == 200
        assert r.json()["message"] == "NOTIFICATION_SKIPPED"
        send_mock.assert_not_called()
    app.dependency_overrides.clear()


def test_41_first_support_message_without_prior(engine, db_session):
    _seed_user(db_session, USER_ID, fcm_token="t")
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=SUPPORT_ID, receiver=USER_ID),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
            return_value={"success": True, "message": "NOTIFICATION_SENT"},
        ):
            r = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{USER_ID}", "messageId": "-Nfirst"},
                headers=_auth_headers(),
            )
        assert r.status_code == 200
        assert r.json()["message"] == "NOTIFICATION_SENT"
    app.dependency_overrides.clear()


def test_42_43_phone_rotation_and_reuse(client, db_session, engine):
    _seed_user(db_session, USER_ID)
    old_support = "7777000001"
    _seed_user(db_session, old_support)
    _seed_support_config(db_session)  # current SUPPORT_ID

    # Stale thread targeting old support after rotation
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(receiver=old_support),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nrot")
    assert r.status_code == 403

    # Reused phone as normal user does not gain support operator identity
    c, app = _make_client(engine, old_support)
    with c:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(sender=old_support, receiver=USER_ID),
        ):
            r2 = c.post(
                "/chat/notifications",
                json={"threadId": f"admin-{USER_ID}", "messageId": "-Nreuse"},
                headers=_auth_headers(),
            )
        assert r2.status_code == 403
        assert r2.json()["detail"] == "INVALID_SUPPORT_CHAT"
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Client cannot override title/body; OpenAPI schema
# ---------------------------------------------------------------------------


def test_55_client_cannot_override_title_body(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    r = client.post(
        "/chat/notifications",
        json={
            "threadId": f"admin-{USER_ID}",
            "messageId": MESSAGE_ID,
            "title": "HACK",
            "body": "HACK",
            "url": "evil",
            "fcmToken": "tok",
            "admin": True,
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_56_58_idempotency(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r1 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nidem")
        r2 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nidem")
    assert r1.status_code == 200
    assert r1.json()["message"] == "NOTIFICATION_SENT"
    assert r2.status_code == 200
    assert r2.json()["message"] == "ALREADY_HANDLED"
    assert send_mock.call_count == 1


def test_59_user_sender_rate_limit(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    orig_sender = chat_svc._SUPPORT_USER_SENDER_MAX
    orig_pair = chat_svc._SUPPORT_USER_PAIR_MAX
    chat_svc._SUPPORT_USER_SENDER_MAX = 2
    chat_svc._SUPPORT_USER_PAIR_MAX = 100
    try:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
            return_value={"success": True, "message": "NOTIFICATION_SENT"},
        ):
            assert (
                _notify(
                    client, thread_id=f"admin-{USER_ID}", message_id="-Nrl0"
                ).status_code
                == 200
            )
            assert (
                _notify(
                    client, thread_id=f"admin-{USER_ID}", message_id="-Nrl1"
                ).status_code
                == 200
            )
            r_lim = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-NrlX")
        assert r_lim.status_code == 429
        assert r_lim.json()["detail"] == "CHAT_NOTIFICATION_RATE_LIMITED"
    finally:
        chat_svc._SUPPORT_USER_SENDER_MAX = orig_sender
        chat_svc._SUPPORT_USER_PAIR_MAX = orig_pair


def test_60_user_support_pair_rate_limit(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    orig_sender = chat_svc._SUPPORT_USER_SENDER_MAX
    orig_pair = chat_svc._SUPPORT_USER_PAIR_MAX
    chat_svc._SUPPORT_USER_SENDER_MAX = 100
    chat_svc._SUPPORT_USER_PAIR_MAX = 1
    try:
        with patch(
            "app_v1.services.chat_notifications.get_chat_message",
            return_value=_support_rtdb(),
        ), patch(
            "app_v1.services.chat_notifications.send_notification_to_token",
            return_value={"success": True, "message": "NOTIFICATION_SENT"},
        ):
            assert (
                _notify(
                    client, thread_id=f"admin-{USER_ID}", message_id="-Npair0"
                ).status_code
                == 200
            )
            r_pair = _notify(
                client, thread_id=f"admin-{USER_ID}", message_id="-Npair1"
            )
        assert r_pair.status_code == 429
    finally:
        chat_svc._SUPPORT_USER_SENDER_MAX = orig_sender
        chat_svc._SUPPORT_USER_PAIR_MAX = orig_pair


def test_61_support_operator_rate_limit(engine, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    orig_op = chat_svc._SUPPORT_OP_SENDER_MAX
    orig_op_pair = chat_svc._SUPPORT_OP_PAIR_MAX
    chat_svc._SUPPORT_OP_SENDER_MAX = 1
    chat_svc._SUPPORT_OP_PAIR_MAX = 100
    try:
        c, app = _make_client(engine, SUPPORT_ID)
        with c:
            with patch(
                "app_v1.services.chat_notifications.get_chat_message",
                return_value=_support_rtdb(sender=SUPPORT_ID, receiver=USER_ID),
            ), patch(
                "app_v1.services.chat_notifications.send_notification_to_token",
                return_value={"success": True, "message": "NOTIFICATION_SENT"},
            ):
                r1 = c.post(
                    "/chat/notifications",
                    json={"threadId": f"admin-{USER_ID}", "messageId": "-Nop1"},
                    headers=_auth_headers(),
                )
                r2 = c.post(
                    "/chat/notifications",
                    json={"threadId": f"admin-{USER_ID}", "messageId": "-Nop2"},
                    headers=_auth_headers(),
                )
            assert r1.status_code == 200
            assert r2.status_code == 429
        app.dependency_overrides.clear()
    finally:
        chat_svc._SUPPORT_OP_SENDER_MAX = orig_op
        chat_svc._SUPPORT_OP_PAIR_MAX = orig_op_pair


def test_62_support_recipient_pair_rate_limit(engine, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    orig_op = chat_svc._SUPPORT_OP_SENDER_MAX
    orig_op_pair = chat_svc._SUPPORT_OP_PAIR_MAX
    chat_svc._SUPPORT_OP_SENDER_MAX = 100
    chat_svc._SUPPORT_OP_PAIR_MAX = 1
    try:
        c, app = _make_client(engine, SUPPORT_ID)
        with c:
            with patch(
                "app_v1.services.chat_notifications.get_chat_message",
                return_value=_support_rtdb(sender=SUPPORT_ID, receiver=USER_ID),
            ), patch(
                "app_v1.services.chat_notifications.send_notification_to_token",
                return_value={"success": True, "message": "NOTIFICATION_SENT"},
            ):
                r1 = c.post(
                    "/chat/notifications",
                    json={"threadId": f"admin-{USER_ID}", "messageId": "-NopP0"},
                    headers=_auth_headers(),
                )
                r2 = c.post(
                    "/chat/notifications",
                    json={"threadId": f"admin-{USER_ID}", "messageId": "-NopP1"},
                    headers=_auth_headers(),
                )
            assert r1.status_code == 200
            assert r2.status_code == 429
        app.dependency_overrides.clear()
    finally:
        chat_svc._SUPPORT_OP_SENDER_MAX = orig_op
        chat_svc._SUPPORT_OP_PAIR_MAX = orig_op_pair


def test_63_64_auth_and_config_failure_do_not_consume_idempotency(client, db_session):
    _seed_user(db_session, USER_ID)
    # No support config
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Ncfg")
    assert r.status_code == 503

    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r2 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Ncfg")
    assert r2.status_code == 200
    assert r2.json()["message"] == "NOTIFICATION_SENT"
    send_mock.assert_called_once()

    # Auth failure (forged) then success with different message should work
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(receiver=OTHER_USER),
    ):
        r3 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nauthf")
    assert r3.status_code == 403
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r4 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nauthf")
    assert r4.status_code == 200


def test_65_different_message_ids_independent(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r1 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Na")
        r2 = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nb")
    assert r1.json()["message"] == "NOTIFICATION_SENT"
    assert r2.json()["message"] == "NOTIFICATION_SENT"
    assert send_mock.call_count == 2


def test_66_67_provider_failure_safe(client, db_session):
    _seed_user(db_session, USER_ID)
    _seed_support_config(db_session)
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value=_support_rtdb(),
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        side_effect=RuntimeError("firebase credential secret-xyz"),
    ):
        r = _notify(client, thread_id=f"admin-{USER_ID}", message_id="-Nprov")
    assert r.status_code == 500
    assert r.json()["detail"] == "CHAT_NOTIFICATION_FAILED"
    assert "credential" not in r.text
    assert "secret-xyz" not in r.text


# ---------------------------------------------------------------------------
# PR26 / PR25 regression
# ---------------------------------------------------------------------------


def test_74_78_peer_regression(client, db_session):
    _seed_user(db_session, PEER_VENDOR, full_name="Alice")
    _seed_user(db_session, PEER_CUSTOMER, full_name="Bob", fcm_token="bob-tok")
    _seed_relationship(
        db_session, customer=PEER_CUSTOMER, vendor=PEER_VENDOR, status="BID - CONFIRMED"
    )
    thread = f"{PEER_VENDOR}-{PEER_CUSTOMER}"
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value={
            "sender": PEER_VENDOR,
            "receiver": PEER_CUSTOMER,
            "text": "Hello peer preview text that should appear sanitized",
            "type": "Text",
        },
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock:
        r = _notify(client, thread_id=thread, message_id="-Npeer1")
    assert r.status_code == 200
    assert r.json()["message"] == "NOTIFICATION_SENT"
    body = send_mock.call_args.kwargs["body"]
    assert "Hello peer preview" in body
    assert body != "Sent you a message"

    # Invalid peer relationship
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value={
            "sender": PEER_VENDOR,
            "receiver": "9000000009",
            "text": "x",
            "type": "Text",
        },
    ):
        # Need matching thread format
        bad_thread = f"{PEER_VENDOR}-9000000009"
        r2 = _notify(client, thread_id=bad_thread, message_id="-Npeer2")
    assert r2.status_code == 403

    # Cancelled
    db_session.query(Request).update({"requestStatus": "BID - CANCELLED"})
    db_session.commit()
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value={
            "sender": PEER_VENDOR,
            "receiver": PEER_CUSTOMER,
            "text": "x",
            "type": "Text",
        },
    ):
        r3 = _notify(client, thread_id=thread, message_id="-Npeer3")
    assert r3.status_code == 403

    # Peer idempotency
    db_session.query(Request).update({"requestStatus": "BID - CONFIRMED"})
    db_session.commit()
    with patch(
        "app_v1.services.chat_notifications.get_chat_message",
        return_value={
            "sender": PEER_VENDOR,
            "receiver": PEER_CUSTOMER,
            "text": "again",
            "type": "Text",
        },
    ), patch(
        "app_v1.services.chat_notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ) as send_mock2:
        a = _notify(client, thread_id=thread, message_id="-NpeerIdem")
        b = _notify(client, thread_id=thread, message_id="-NpeerIdem")
    assert a.json()["message"] == "NOTIFICATION_SENT"
    assert b.json()["message"] == "ALREADY_HANDLED"
    assert send_mock2.call_count == 1


def test_79_80_pr25_generic_routes(client, db_session):
    _seed_user(db_session, PEER_CUSTOMER)
    r = client.post(
        "/notificationtodriver",
        json={
            "title": "T",
            "body": "B",
            "userAppId": PEER_CUSTOMER,
            "url": "///Open Requests",
        },
        headers={"Authorization": "Bearer test-jwt"},
    )
    assert r.status_code == 403

    with patch(
        "app_v1.services.notifications.send_notification_to_token",
        return_value={"success": True, "message": "NOTIFICATION_SENT"},
    ):
        r2 = client.post(
            "/notificationtodriver",
            json={
                "title": "T",
                "body": "B",
                "userAppId": PEER_CUSTOMER,
                "url": "///Open Requests",
            },
            headers={
                "Authorization": "Bearer test-jwt",
                internal_auth.INTERNAL_NOTIFICATION_HEADER: os.environ[
                    "INTERNAL_NOTIFICATION_KEY"
                ],
            },
        )
    assert r2.status_code == 200


def test_81_83_openapi_contract(client):
    schema = client.app.openapi()
    paths = schema["paths"]
    assert "/chat/support/config" in paths
    assert "get" in paths["/chat/support/config"]
    assert "/chat/notifications" in paths
    post = paths["/chat/notifications"]["post"]
    # Resolve request body schema
    content = post["requestBody"]["content"]["application/json"]["schema"]
    ref = content.get("$ref")
    if ref:
        name = ref.split("/")[-1]
        props = schema["components"]["schemas"][name]["properties"]
        assert set(props.keys()) == {"threadId", "messageId"}
        assert schema["components"]["schemas"][name].get("additionalProperties") is False or (
            "title" not in props and "fcmToken" not in props and "body" not in props
        )
    else:
        assert set(content["properties"].keys()) == {"threadId", "messageId"}


def test_no_db_close_misuse_in_resolver_and_endpoint():
    resolver = Path(ROOT / "app_v1/crud/admin_number.py").read_text()
    endpoint = Path(ROOT / "app_v1/endpoints/chat.py").read_text()
    service = Path(ROOT / "app_v1/services/chat_notifications.py").read_text()
    assert "db.close()" not in resolver
    assert "db.close()" not in endpoint
    assert "db.close()" not in service
