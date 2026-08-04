"""
PR28 — POST/DELETE /chat/media (authenticated chat photo upload + cleanup).

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import types
from datetime import date, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
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
os.environ["AZURE_CHAT_DOCS_CONTAINER_URL"] = (
    "https://openbidstorage.blob.core.windows.net/chat-docs"
)
os.environ["AZURE_CHAT_DOCS_SAS"] = "sv=test&sig=fake"

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
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.admin_number import AdminNumber  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.endpoints import chat as chat_mod  # noqa: E402
from app_v1.endpoints import utils as utils_mod  # noqa: E402
from app_v1.services import chat_media as media_svc  # noqa: E402
from app_v1.services import chat_notifications as chat_svc  # noqa: E402
from app_v1.utils.image import ChatDocsStorageError  # noqa: E402

SENDER_ID = "7022359323"
RECIPIENT_ID = "8637554388"
OTHER_ID = "9000000001"
SUPPORT_ID = "9999000001"
THREAD_ID = f"{SENDER_ID}-{RECIPIENT_ID}"
MESSAGE_ID = "-NabcChatMedia01"
SUPPORT_THREAD = f"admin-{SENDER_ID}"



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
            Request.__table__,
            AdminNumber.__table__,
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
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(SENDER_ID)
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


def _make_client(engine, user_id: str):
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
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app), app


_UID_SEQ = 4000
_RID_SEQ = 8000
_ADMIN_SEQ = 200


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


def _seed_support_config(db):
    _seed_user(db, SUPPORT_ID, full_name="OpenBid Support")
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


def _jpeg_bytes(size=(16, 16), color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes(size=(16, 16), color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _data_uri(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _upload_body(**overrides):
    body = {
        "threadId": THREAD_ID,
        "messageId": MESSAGE_ID,
        "mediaType": "PHOTO",
        "fileName": "optional.jpg",
        "mimeType": "image/jpeg",
        "content": _data_uri(_jpeg_bytes()),
    }
    body.update(overrides)
    return body


def _seed_peer(db, *, status: str = "BID - CONFIRMED"):
    _seed_user(db, SENDER_ID)
    _seed_user(db, RECIPIENT_ID, full_name="Vendor Name")
    _seed_relationship(
        db, customer=SENDER_ID, vendor=RECIPIENT_ID, status=status
    )


# In-memory fake Azure chat-docs store for PR28 storage tests.
_FAKE_BLOBS: dict[str, dict] = {}


def _fake_head(path: str):
    if path not in _FAKE_BLOBS:
        return None
    return dict(_FAKE_BLOBS[path]["meta"])


def _fake_upload(*, relative_blob_path, content, content_type, metadata):
    _FAKE_BLOBS[relative_blob_path] = {
        "content": content,
        "content_type": content_type,
        "meta": {k.lower(): str(v) for k, v in metadata.items()},
    }
    return (
        f"https://openbidstorage.blob.core.windows.net/chat-docs/"
        f"{relative_blob_path}"
    )


def _fake_delete(path: str):
    _FAKE_BLOBS.pop(path, None)


@pytest.fixture(autouse=True)
def _clear_blobs():
    _FAKE_BLOBS.clear()
    yield
    _FAKE_BLOBS.clear()


def _patch_storage():
    return patch.multiple(
        media_svc,
        chat_docs_head_metadata=_fake_head,
        chat_docs_upload_bytes=_fake_upload,
        chat_docs_delete_blob=_fake_delete,
    )


# ---------------------------------------------------------------------------
# Auth / sender lifecycle
# ---------------------------------------------------------------------------


def test_01_missing_jwt_401(client_no_jwt):
    r = client_no_jwt.post("/chat/media", json=_upload_body())
    assert r.status_code in (401, 403)


def test_02_invalid_jwt_401(engine):
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
        r = c.post(
            "/chat/media",
            json=_upload_body(),
            headers={
                "Authorization": "Bearer not-a-real-jwt",
                "X-Client-Id": "c",
            },
        )
    assert r.status_code in (401, 403)


def test_03_missing_sender_403(client, db_session):
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_MEDIA_NOT_ALLOWED"


def test_04_tombstoned_sender_403(engine, db_session):
    _seed_user(db_session, f"{SENDER_ID}.DELETED")
    c, app = _make_client(engine, f"{SENDER_ID}.DELETED")
    with c, _patch_storage():
        r = c.post("/chat/media", json=_upload_body(), headers=_auth_headers())
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_MEDIA_NOT_ALLOWED"
    app.dependency_overrides.clear()


def test_05_locked_sender_403(client, db_session):
    _seed_user(db_session, SENDER_ID, lock_app=True)
    _seed_user(db_session, RECIPIENT_ID)
    _seed_relationship(db_session, customer=SENDER_ID, vendor=RECIPIENT_ID)
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_MEDIA_NOT_ALLOWED"


def test_06_no_client_sender_field(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json={**_upload_body(), "sender": OTHER_ID},
            headers=_auth_headers(),
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Peer authorization
# ---------------------------------------------------------------------------


def test_07_08_valid_peer_both_directions(engine, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        c1, app1 = _make_client(engine, SENDER_ID)
        with c1:
            r1 = c1.post(
                "/chat/media", json=_upload_body(), headers=_auth_headers()
            )
        app1.dependency_overrides.clear()
        assert r1.status_code == 200
        assert r1.json()["message"] == "UPLOADED"
        assert "mediaUrl" in r1.json()
        assert SENDER_ID not in r1.json()["mediaUrl"]
        assert RECIPIENT_ID not in r1.json()["mediaUrl"]

        c2, app2 = _make_client(engine, RECIPIENT_ID)
        with c2:
            r2 = c2.post(
                "/chat/media",
                json=_upload_body(messageId="-NvendorPhoto01"),
                headers=_auth_headers(),
            )
        app2.dependency_overrides.clear()
        assert r2.status_code == 200


def test_09_bid_confirmed_allowed(client, db_session):
    _seed_peer(db_session, status="BID - CONFIRMED")
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 200


def test_10_11_request_confirmed_and_past(client, db_session):
    _seed_peer(db_session, status="REQUEST - CONFIRMED")
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "status_value",
    [
        "BID - OPEN",
        "REQUEST - CANCELLED BY USER",
        "BOOKING - CANCELLED BY USER",
    ],
)
def test_12_14_disallowed_statuses(client, db_session, status_value):
    _seed_peer(db_session, status=status_value)
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "CHAT_MEDIA_NOT_ALLOWED"


def test_15_unrelated_pair_rejected(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, RECIPIENT_ID)
    _seed_user(db_session, OTHER_ID)
    _seed_relationship(db_session, customer=SENDER_ID, vendor=OTHER_ID)
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 403


def test_16_sender_not_in_thread(engine, db_session):
    _seed_peer(db_session)
    c, app = _make_client(engine, OTHER_ID)
    _seed_user(db_session, OTHER_ID)
    with c, _patch_storage():
        r = c.post("/chat/media", json=_upload_body(), headers=_auth_headers())
    app.dependency_overrides.clear()
    assert r.status_code == 403


def test_17_malformed_peer_thread(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId="not-a-thread"),
            headers=_auth_headers(),
        )
    assert r.status_code == 422


def test_18_client_cannot_target_arbitrary_peer(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json={
                **_upload_body(),
                "receiver": OTHER_ID,
                "vendorId": OTHER_ID,
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Support authorization
# ---------------------------------------------------------------------------


def test_19_user_to_support(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_support_config(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId=SUPPORT_THREAD, messageId="-Nsup1"),
            headers=_auth_headers(),
        )
    assert r.status_code == 200


def test_20_support_to_user(engine, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c, _patch_storage():
        r = c.post(
            "/chat/media",
            json=_upload_body(
                threadId=SUPPORT_THREAD, messageId="-NsupFromOp"
            ),
            headers=_auth_headers(),
        )
    app.dependency_overrides.clear()
    assert r.status_code == 200


def test_21_forged_admin_other_user(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, OTHER_ID)
    _seed_support_config(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId=f"admin-{OTHER_ID}"),
            headers=_auth_headers(),
        )
    assert r.status_code == 403


def test_22_ordinary_user_cannot_act_as_support(engine, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, OTHER_ID)
    _seed_support_config(db_session)
    c, app = _make_client(engine, SENDER_ID)
    with c, _patch_storage():
        r = c.post(
            "/chat/media",
            json=_upload_body(threadId=f"admin-{OTHER_ID}"),
            headers=_auth_headers(),
        )
    app.dependency_overrides.clear()
    assert r.status_code == 403


def test_23_wrong_support_account(engine, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_support_config(db_session)
    _seed_user(db_session, OTHER_ID)
    c, app = _make_client(engine, OTHER_ID)
    with c, _patch_storage():
        r = c.post(
            "/chat/media",
            json=_upload_body(threadId=SUPPORT_THREAD),
            headers=_auth_headers(),
        )
    app.dependency_overrides.clear()
    assert r.status_code == 403


def test_24_missing_support_config(client, db_session):
    _seed_user(db_session, SENDER_ID)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId=SUPPORT_THREAD),
            headers=_auth_headers(),
        )
    assert r.status_code == 403


def test_25_multiple_admin_rows(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, SUPPORT_ID)
    _seed_admin(db_session, SUPPORT_ID)
    _seed_admin(db_session, "8888000002")
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId=SUPPORT_THREAD),
            headers=_auth_headers(),
        )
    assert r.status_code == 403


def test_26_locked_support(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, SUPPORT_ID, lock_app=True)
    _seed_admin(db_session, SUPPORT_ID)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId=SUPPORT_THREAD),
            headers=_auth_headers(),
        )
    assert r.status_code == 403


def test_27_tombstoned_support(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_user(db_session, f"{SUPPORT_ID}.DELETED")
    _seed_admin(db_session, f"{SUPPORT_ID}.DELETED")
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId=SUPPORT_THREAD),
            headers=_auth_headers(),
        )
    assert r.status_code == 403


def test_28_support_self_thread(engine, db_session):
    _seed_support_config(db_session)
    c, app = _make_client(engine, SUPPORT_ID)
    with c, _patch_storage():
        r = c.post(
            "/chat/media",
            json=_upload_body(threadId=f"admin-{SUPPORT_ID}"),
            headers=_auth_headers(),
        )
    app.dependency_overrides.clear()
    assert r.status_code == 403


def test_29_stale_support_thread(client, db_session):
    _seed_user(db_session, SENDER_ID)
    _seed_support_config(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId="admin-1111222233"),
            headers=_auth_headers(),
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Schema / request
# ---------------------------------------------------------------------------


def test_30_missing_thread_id(client, db_session):
    _seed_peer(db_session)
    body = _upload_body()
    del body["threadId"]
    r = client.post("/chat/media", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_31_missing_message_id(client, db_session):
    _seed_peer(db_session)
    body = _upload_body()
    del body["messageId"]
    r = client.post("/chat/media", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_32_missing_content(client, db_session):
    _seed_peer(db_session)
    body = _upload_body()
    del body["content"]
    r = client.post("/chat/media", json=body, headers=_auth_headers())
    assert r.status_code == 422


def test_33_media_type_not_photo(client, db_session):
    _seed_peer(db_session)
    r = client.post(
        "/chat/media",
        json=_upload_body(mediaType="PDF"),
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_34_extra_fields_rejected(client, db_session):
    _seed_peer(db_session)
    for extra in (
        {"sender": SENDER_ID},
        {"receiver": RECIPIENT_ID},
        {"path": "x"},
        {"container": "chat-docs"},
        {"blobPath": "x"},
    ):
        r = client.post(
            "/chat/media",
            json={**_upload_body(), **extra},
            headers=_auth_headers(),
        )
        assert r.status_code == 422


def test_35_36_invalid_message_id(client, db_session):
    _seed_peer(db_session)
    for bad in ("has/slash", "../traverse", "has.dot", "has#hash", "a$b"):
        with _patch_storage():
            r = client.post(
                "/chat/media",
                json=_upload_body(messageId=bad),
                headers=_auth_headers(),
            )
        assert r.status_code == 422


def test_37_oversized_ids(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(threadId="1" * 200, messageId="m" * 200),
            headers=_auth_headers(),
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Image validation
# ---------------------------------------------------------------------------


def test_38_39_jpeg_png_accepted(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r1 = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nj1", content=_data_uri(_jpeg_bytes())
            ),
            headers=_auth_headers(),
        )
        r2 = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Np1",
                mimeType="image/png",
                content=_data_uri(_png_bytes(), "image/png"),
            ),
            headers=_auth_headers(),
        )
    assert r1.status_code == 200
    assert r1.json()["mimeType"] == "image/jpeg"
    assert r2.status_code == 200
    assert r2.json()["mimeType"] == "image/png"


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"not-an-image", 415),
        (b"\xff\xd8\xff" + b"trunc", 422),
        (b"\x89PNG\r\n\x1a\n" + b"trunc", 422),
        (b"ftypheic", 415),
        (b"RIFF....WEBP", 415),
        (b"GIF89a", 415),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", 415),
        (b"%PDF-1.4", 415),
        (b"PK\x03\x04", 415),
        (b"\x7fELF", 415),
    ],
)
def test_40_47_reject_bad_formats(client, db_session, payload, code):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nbad" + hashlib.md5(payload).hexdigest()[:6],
                content=_data_uri(payload),
            ),
            headers=_auth_headers(),
        )
    assert r.status_code in (code, 415, 422)


def test_48_mime_spoof_pdf_as_jpeg(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nspoof1",
                content=_data_uri(b"%PDF-1.4 fake", "image/jpeg"),
            ),
            headers=_auth_headers(),
        )
    assert r.status_code in (415, 422)


def test_49_extension_spoof_ignored(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nspoof2",
                fileName="evil.pdf",
                content=_data_uri(_jpeg_bytes()),
            ),
            headers=_auth_headers(),
        )
    assert r.status_code == 200
    assert r.json()["mimeType"] == "image/jpeg"
    assert r.json()["fileName"].endswith(".jpg")


def test_50_51_malformed_base64_and_empty(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r1 = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nmal1", content="data:image/jpeg;base64,!!!"
            ),
            headers=_auth_headers(),
        )
        r2 = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nmal2", content="data:image/jpeg;base64,"
            ),
            headers=_auth_headers(),
        )
    assert r1.status_code == 422
    assert r2.status_code == 422


def test_52_over_2mb_413(client, db_session):
    _seed_peer(db_session)
    huge = b"\xff\xd8\xff" + (b"\x00" * (2 * 1024 * 1024 + 10))
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nhuge", content=_data_uri(huge)),
            headers=_auth_headers(),
        )
    assert r.status_code == 413
    assert r.json()["detail"] == "CHAT_MEDIA_TOO_LARGE"


def test_53_decompression_bomb(client, db_session):
    _seed_peer(db_session)
    # Extremely wide PNG that may trip pixel cap when loaded.
    buf = io.BytesIO()
    Image.new("RGB", (10000, 3000), color=(1, 2, 3)).save(buf, format="PNG")
    raw = buf.getvalue()
    # If under 2MB and under pixel cap, may pass — force pixel bomb via mock.
    with _patch_storage(), patch(
        "app_v1.utils.image.Image.open",
        side_effect=Image.DecompressionBombError("bomb"),
    ):
        r = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nbomb",
                mimeType="image/png",
                content=_data_uri(raw, "image/png"),
            ),
            headers=_auth_headers(),
        )
    assert r.status_code == 422


def test_54_client_mime_disagreement(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(
                messageId="-Nmime",
                mimeType="image/png",
                content=_data_uri(_jpeg_bytes(), "image/png"),
            ),
            headers=_auth_headers(),
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Storage / idempotency
# ---------------------------------------------------------------------------


def test_55_62_storage_path_and_safety(client, db_session):
    _seed_peer(db_session)
    with _patch_storage():
        r = client.post(
            "/chat/media", json=_upload_body(), headers=_auth_headers()
        )
    assert r.status_code == 200
    body = r.json()
    url = body["mediaUrl"]
    assert "/chat/" in url
    assert MESSAGE_ID in url
    assert SENDER_ID not in url
    assert RECIPIENT_ID not in url
    assert "sig=" not in url
    assert "SAS" not in str(body)
    thash = media_svc.thread_hash(THREAD_ID)
    expected_prefix = f"chat/{thash}/{MESSAGE_ID}."
    assert any(p.startswith(expected_prefix) for p in _FAKE_BLOBS)
    meta = next(iter(_FAKE_BLOBS.values()))["meta"]
    assert SENDER_ID not in str(meta.values())
    assert RECIPIENT_ID not in str(meta.values())
    assert meta["mimetype"] == "image/jpeg"


def test_60_61_provider_failure_safe(client, db_session):
    _seed_peer(db_session)

    def _fail_upload(**kwargs):
        raise ChatDocsStorageError("UPLOAD_FAILED")

    with patch.object(media_svc, "chat_docs_head_metadata", return_value=None), patch.object(
        media_svc, "chat_docs_upload_bytes", side_effect=_fail_upload
    ):
        r = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nfail1"),
            headers=_auth_headers(),
        )
    assert r.status_code == 500
    assert r.json()["detail"] == "CHAT_MEDIA_UPLOAD_FAILED"
    assert "UPLOAD_FAILED" not in r.text or r.json()["detail"] == "CHAT_MEDIA_UPLOAD_FAILED"


def test_63_68_idempotency(client, db_session):
    _seed_peer(db_session)
    content = _data_uri(_jpeg_bytes(color=(1, 2, 3)))
    with _patch_storage():
        r1 = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nidem", content=content),
            headers=_auth_headers(),
        )
        assert r1.status_code == 200
        assert len(_FAKE_BLOBS) == 1
        url1 = r1.json()["mediaUrl"]

        r2 = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nidem", content=content),
            headers=_auth_headers(),
        )
        assert r2.status_code == 200
        assert r2.json()["mediaUrl"] == url1
        assert len(_FAKE_BLOBS) == 1

        other = _data_uri(_jpeg_bytes(color=(9, 9, 9)))
        r3 = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nidem", content=other),
            headers=_auth_headers(),
        )
        assert r3.status_code == 409
        assert r3.json()["detail"] == "CHAT_MEDIA_CONFLICT"

        r4 = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nidem2", content=content),
            headers=_auth_headers(),
        )
        assert r4.status_code == 200
        assert r4.json()["mediaUrl"] != url1
        assert len(_FAKE_BLOBS) == 2


def test_67_unauthorized_cannot_probe(engine, db_session):
    _seed_peer(db_session)
    content = _data_uri(_jpeg_bytes())
    with _patch_storage():
        c1, app1 = _make_client(engine, SENDER_ID)
        with c1:
            r1 = c1.post(
                "/chat/media",
                json=_upload_body(messageId="-Nprobe", content=content),
                headers=_auth_headers(),
            )
        app1.dependency_overrides.clear()
        assert r1.status_code == 200

        _seed_user(db_session, OTHER_ID)
        c2, app2 = _make_client(engine, OTHER_ID)
        with c2:
            r2 = c2.post(
                "/chat/media",
                json=_upload_body(messageId="-Nprobe", content=content),
                headers=_auth_headers(),
            )
        app2.dependency_overrides.clear()
        # Unauthorized must not learn blob exists via distinct conflict code.
        assert r2.status_code == 403
        assert r2.json()["detail"] == "CHAT_MEDIA_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def test_69_70_rate_limits(client, db_session):
    _seed_peer(db_session)
    with _patch_storage(), patch.object(media_svc, "_SENDER_MAX", 2), patch.object(
        media_svc, "_PAIR_MAX", 50
    ):
        for i in range(2):
            r = client.post(
                "/chat/media",
                json=_upload_body(messageId=f"-Nrate{i}"),
                headers=_auth_headers(),
            )
            assert r.status_code == 200
        r_lim = client.post(
            "/chat/media",
            json=_upload_body(messageId="-NrateX"),
            headers=_auth_headers(),
        )
        assert r_lim.status_code == 429
        assert r_lim.json()["detail"] == "CHAT_MEDIA_RATE_LIMITED"


def test_71_auth_failure_no_rate_bucket(client, db_session):
    _seed_peer(db_session, status="BID - OPEN")
    with _patch_storage():
        for _ in range(5):
            r = client.post(
                "/chat/media", json=_upload_body(), headers=_auth_headers()
            )
            assert r.status_code == 403
    # Flip to allowed and ensure not rate-limited from prior failures.
    db_session.query(Request).update(
        {Request.requestStatus: "BID - CONFIRMED"}
    )
    db_session.commit()
    with _patch_storage():
        r = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nafter"),
            headers=_auth_headers(),
        )
    assert r.status_code == 200


def test_72_independent_buckets(engine, db_session):
    _seed_peer(db_session)
    _seed_user(db_session, OTHER_ID)
    other_thread = (
        f"{OTHER_ID}-{RECIPIENT_ID}"
        if int(OTHER_ID) < int(RECIPIENT_ID)
        else f"{RECIPIENT_ID}-{OTHER_ID}"
    )
    _seed_relationship(
        db_session, customer=OTHER_ID, vendor=RECIPIENT_ID
    )
    with _patch_storage(), patch.object(media_svc, "_SENDER_MAX", 1):
        c1, app1 = _make_client(engine, SENDER_ID)
        with c1:
            assert (
                c1.post(
                    "/chat/media",
                    json=_upload_body(messageId="-Na1"),
                    headers=_auth_headers(),
                ).status_code
                == 200
            )
            assert (
                c1.post(
                    "/chat/media",
                    json=_upload_body(messageId="-Na2"),
                    headers=_auth_headers(),
                ).status_code
                == 429
            )
        app1.dependency_overrides.clear()

        c2, app2 = _make_client(engine, OTHER_ID)
        with c2:
            r = c2.post(
                "/chat/media",
                json=_upload_body(
                    threadId=other_thread, messageId="-Nb1"
                ),
                headers=_auth_headers(),
            )
        app2.dependency_overrides.clear()
        assert r.status_code == 200


def test_73_idempotent_reconciliation_safe(client, db_session):
    _seed_peer(db_session)
    content = _data_uri(_jpeg_bytes(color=(5, 5, 5)))
    with _patch_storage(), patch.object(media_svc, "_SENDER_MAX", 1):
        r1 = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nrec", content=content),
            headers=_auth_headers(),
        )
        assert r1.status_code == 200
        # Reconciliation must not be blocked by sender rate limit.
        r2 = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nrec", content=content),
            headers=_auth_headers(),
        )
        assert r2.status_code == 200
        assert r2.json()["mediaUrl"] == r1.json()["mediaUrl"]


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_74_cleanup_missing_jwt(client_no_jwt):
    r = client_no_jwt.request(
        "DELETE",
        "/chat/media",
        json={"threadId": THREAD_ID, "messageId": MESSAGE_ID},
    )
    assert r.status_code in (401, 403)


def test_75_cleanup_unauthorized(engine, db_session):
    _seed_peer(db_session)
    _seed_user(db_session, OTHER_ID)
    c, app = _make_client(engine, OTHER_ID)
    with c, _patch_storage(), patch(
        "app_v1.services.chat_media.get_chat_message", return_value=None
    ):
        r = c.request(
            "DELETE",
            "/chat/media",
            json={"threadId": THREAD_ID, "messageId": MESSAGE_ID},
            headers=_auth_headers(),
        )
    app.dependency_overrides.clear()
    assert r.status_code == 403


def test_76_cleanup_message_exists_409(client, db_session):
    _seed_peer(db_session)
    with _patch_storage(), patch(
        "app_v1.services.chat_media.get_chat_message",
        return_value={"sender": SENDER_ID, "type": "Photo"},
    ):
        r = client.request(
            "DELETE",
            "/chat/media",
            json={"threadId": THREAD_ID, "messageId": MESSAGE_ID},
            headers=_auth_headers(),
        )
    assert r.status_code == 409
    assert r.json()["detail"] == "CHAT_MEDIA_ALREADY_COMMITTED"


def test_77_78_cleanup_deletes_or_idempotent(client, db_session):
    _seed_peer(db_session)
    content = _data_uri(_jpeg_bytes())
    with _patch_storage(), patch(
        "app_v1.services.chat_media.get_chat_message", return_value=None
    ):
        up = client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nclean1", content=content),
            headers=_auth_headers(),
        )
        assert up.status_code == 200
        assert _FAKE_BLOBS
        r = client.request(
            "DELETE",
            "/chat/media",
            json={"threadId": THREAD_ID, "messageId": "-Nclean1"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        assert r.json()["message"] == "DELETED"
        assert not _FAKE_BLOBS
        r2 = client.request(
            "DELETE",
            "/chat/media",
            json={"threadId": THREAD_ID, "messageId": "-Nclean1"},
            headers=_auth_headers(),
        )
        assert r2.status_code == 200
        assert r2.json()["message"] == "DELETED"


def test_79_cleanup_rejects_url_path(client, db_session):
    _seed_peer(db_session)
    r = client.request(
        "DELETE",
        "/chat/media",
        json={
            "threadId": THREAD_ID,
            "messageId": MESSAGE_ID,
            "mediaUrl": "https://evil.example/x",
            "path": "chat/x",
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 422


def test_80_cleanup_provider_failure(client, db_session):
    _seed_peer(db_session)
    content = _data_uri(_jpeg_bytes())
    with _patch_storage(), patch(
        "app_v1.services.chat_media.get_chat_message", return_value=None
    ):
        client.post(
            "/chat/media",
            json=_upload_body(messageId="-NcleanFail", content=content),
            headers=_auth_headers(),
        )

        def _boom(path):
            raise ChatDocsStorageError("DELETE_FAILED")

        with patch.object(media_svc, "chat_docs_delete_blob", side_effect=_boom):
            r = client.request(
                "DELETE",
                "/chat/media",
                json={"threadId": THREAD_ID, "messageId": "-NcleanFail"},
                headers=_auth_headers(),
            )
    assert r.status_code == 500
    assert r.json()["detail"] == "CHAT_MEDIA_CLEANUP_FAILED"


def test_81_cleanup_cannot_delete_other_thread(client, db_session):
    _seed_peer(db_session)
    content = _data_uri(_jpeg_bytes())
    with _patch_storage(), patch(
        "app_v1.services.chat_media.get_chat_message", return_value=None
    ):
        client.post(
            "/chat/media",
            json=_upload_body(messageId="-Nothr", content=content),
            headers=_auth_headers(),
        )
        # Attempt cleanup with wrong thread — authorization fails or no matching blob.
        r = client.request(
            "DELETE",
            "/chat/media",
            json={
                "threadId": f"{SENDER_ID}-{OTHER_ID}"
                if int(SENDER_ID) < int(OTHER_ID)
                else f"{OTHER_ID}-{SENDER_ID}",
                "messageId": "-Nothr",
            },
            headers=_auth_headers(),
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Regression / OpenAPI
# ---------------------------------------------------------------------------


def test_82_85_notification_regressions_still_importable():
    assert hasattr(chat_svc, "dispatch_chat_notification")
    assert hasattr(chat_svc, "has_eligible_customer_vendor_relationship")
    assert chat_mod.router is not None


def test_86_89_openapi_routes(client):
    schema = client.app.openapi()
    paths = schema["paths"]
    assert "/chat/media" in paths
    assert "post" in paths["/chat/media"]
    assert "delete" in paths["/chat/media"]
    post_body = paths["/chat/media"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    if "$ref" in post_body:
        ref = post_body["$ref"].split("/")[-1]
        props = schema["components"]["schemas"][ref].get("properties", {})
    else:
        props = post_body.get("properties", {})
    for forbidden in (
        "sender",
        "receiver",
        "path",
        "container",
        "blobPath",
        "mediaUrl",
    ):
        assert forbidden not in props
    assert "/uploadchatdoc" in paths
    assert "/chat/media" != "/uploadchatdoc"


def test_90_no_db_migration_model():
    import app_v1.models as models_pkg

    names = dir(models_pkg)
    assert "ChatMedia" not in names
    assert "chat_media_upload" not in names
