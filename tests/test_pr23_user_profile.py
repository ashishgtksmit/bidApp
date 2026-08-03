"""
PR23 profile-image upload + GET /getuserdetails ownership hardening.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
import time
import types
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
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

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.crud import user as user_crud  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402
from app_v1.schemas.user_table import UserImageUpload  # noqa: E402

CUSTOMER_ID = "7022359323"
VENDOR_ID = "8637554388"
PENDING_VENDOR = "8637554387"
LOCKED_USER = "7000000001"
OTHER_USER = "7000000002"
MISSING_USER = "7999999999"
TOMBSTONE_USER = "7022359323.DELETED"

PR23_TABLES = [User.__table__]

_GIF = base64.b64encode(
    b"GIF89a\x01\x00\x01\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
    b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
).decode()


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _jpeg_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(200, 100, 50)).save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _webp_b64() -> str | None:
    buf = io.BytesIO()
    try:
        Image.new("RGB", (2, 2), color=(1, 2, 3)).save(buf, format="WEBP")
    except Exception:
        return None
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR23_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr23_client(engine, Session, user_id: str | None):
    app = FastAPI()
    app.include_router(user_router)

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
        emailId=kwargs.get("emailId", f"{user_app_id}@example.com"),
        dob=kwargs.get("dob", "1990-01-01"),
        city=kwargs.get("city", "Gangtok"),
        gender=kwargs.get("gender", "Male"),
        profilePicture=kwargs.get("profilePicture", "images/profilepic_male.png"),
        alsoVendor=kwargs.get("alsoVendor", False),
        vendorApproved=kwargs.get("vendorApproved", False),
        lockApp=kwargs.get("lockApp", False),
        customerRating=kwargs.get("customerRating", "4.5"),
        totalCustomerReviews=kwargs.get("totalCustomerReviews", 12),
        rating=kwargs.get("rating", "4.5"),
        totalNoOfReviews=kwargs.get("totalNoOfReviews", 3),
        fcmToken=kwargs.get("fcmToken", "secret-fcm-token-should-not-leak"),
        joiningDate=kwargs.get("joiningDate", date(2024, 1, 15)),
        tags=kwargs.get("tags", "tag1"),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 12),
        user_login_status=kwargs.get("user_login_status", "LOGGEDOUT"),
        cityPreferences=kwargs.get("cityPreferences", "1"),
        requestTypePreferences=kwargs.get("requestTypePreferences", "2,3"),
        regionPreferences=kwargs.get("regionPreferences", None),
        address=kwargs.get("address", "Line address"),
        state=kwargs.get("state", "Sikkim"),
        bankAccountHolderName=kwargs.get("bankAccountHolderName", "Bank Holder"),
        bankAccountNo=kwargs.get("bankAccountNo", "123456789012"),
        bankIFSC=kwargs.get("bankIFSC", "SBIN0001234"),
        bankName=kwargs.get("bankName", "SBI"),
        imageAadhar=kwargs.get("imageAadhar", "https://example.com/aadhaar.png"),
        imagePAN=kwargs.get("imagePAN", "https://example.com/pan.png"),
        imageBankAccount=kwargs.get(
            "imageBankAccount", "https://example.com/passbook.png"
        ),
        tableTimestamp=kwargs.get("tableTimestamp", datetime(2024, 6, 1, 10, 0, 0)),
    )
    db.add(user)
    db.commit()
    return user


def _image_body(image_b64: str, *, prefix: str = "data:image/jpeg;base64,", **extra):
    payload = {"image": f"{prefix}{image_b64}"}
    payload.update(extra)
    return payload


@contextmanager
def _blob_patches():
    upload_calls: list[dict] = []
    delete_calls: list[str] = []

    def _fake_upload(
        blob_name,
        base64_data,
        make_public=False,
        max_upload_bytes=20 * 1024 * 1024,
    ):
        ext = "png" if "image/png" in str(base64_data)[:40] else "jpg"
        url = f"https://example.blob.core.windows.net/container/{blob_name}.{ext}"
        upload_calls.append(
            {
                "blob_name": blob_name,
                "base64_data": base64_data,
                "make_public": make_public,
                "max_upload_bytes": max_upload_bytes,
            }
        )
        return True, url

    def _fake_delete(url):
        delete_calls.append(str(url))

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "azure_blob_delete_by_url", side_effect=_fake_delete
    ):
        yield upload_calls, delete_calls


def _assert_no_sensitive_response(payload: dict | list, raw_text: str):
    forbidden = {
        "password",
        "bankAccountNo",
        "BANK_AC_NO",
        "imageAadhar",
        "imagePAN",
        "imageBankAccount",
        "AZURE",
        "SAS",
        "AccountKey",
    }
    blob = raw_text
    for token in forbidden:
        assert token not in blob
    assert "123456789012" not in blob
    assert "secret-fcm-token" not in blob
    assert "https://example.com/aadhaar.png" not in blob
    if isinstance(payload, dict):
        assert forbidden.isdisjoint(set(payload.keys()))


@pytest.fixture
def db_session():
    engine, Session = _memory_db()
    db = Session()
    try:
        yield db, engine, Session
    finally:
        db.close()


# --- Auth / ownership -------------------------------------------------------


def test_upload_no_jwt_returns_401_or_403(db_session):
    _, engine, Session = db_session
    client = _pr23_client(engine, Session, user_id=None)
    response = client.post(
        "/profilepageupload",
        json=_image_body(_jpeg_b64()),
    )
    assert response.status_code in (401, 403)


def test_upload_invalid_jwt_path_no_override_returns_401_or_403(db_session):
    _, engine, Session = db_session
    app = FastAPI()
    app.include_router(user_router)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    response = client.post(
        "/profilepageupload",
        json=_image_body(_jpeg_b64()),
    )
    assert response.status_code in (401, 403)


def test_upload_own_succeeds(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "UPLOADED"
    assert body.get("url")
    _assert_no_sensitive_response(body, response.text)


def test_upload_no_user_app_id_required(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200


def test_upload_matching_user_app_id_allowed(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post(
            "/profilepageupload",
            json=_image_body(_jpeg_b64(), userAppId=CUSTOMER_ID),
        )
    assert response.status_code == 200


def test_upload_mismatched_user_app_id_403(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post(
        "/profilepageupload",
        json=_image_body(_jpeg_b64(), userAppId=OTHER_USER),
    )
    assert response.status_code == 403


def test_upload_cannot_target_other_user(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        profilePicture="images/customer.png",
    )
    _add_user(
        db,
        user_app_id=OTHER_USER,
        uid=2,
        profilePicture="images/other.png",
    )
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post(
        "/profilepageupload",
        json=_image_body(_jpeg_b64(), userAppId=OTHER_USER),
    )
    assert response.status_code == 403
    db.expire_all()
    other = db.query(User).filter(User.userAppId == OTHER_USER).one()
    assert other.profilePicture == "images/other.png"


def test_upload_missing_user_404(db_session):
    _, engine, Session = db_session
    client = _pr23_client(engine, Session, MISSING_USER)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 404
    assert response.json()["detail"] == "USER_NOT_FOUND"


def test_upload_tombstone_user_403(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=TOMBSTONE_USER, uid=1)
    client = _pr23_client(engine, Session, TOMBSTONE_USER)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 403
    assert response.json()["detail"] == "PROFILE_UPDATE_NOT_ALLOWED"


def test_upload_locked_user_allowed(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=LOCKED_USER, uid=1, lockApp=True, alsoVendor=True)
    client = _pr23_client(engine, Session, LOCKED_USER)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200


def test_upload_pending_vendor_allowed(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
    )
    client = _pr23_client(engine, Session, PENDING_VENDOR)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200


def test_upload_customer_allowed(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200


def test_upload_vendor_allowed(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=VENDOR_ID,
        uid=1,
        alsoVendor=True,
        vendorApproved=True,
    )
    client = _pr23_client(engine, Session, VENDOR_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200


# --- Media validation -------------------------------------------------------


def test_upload_valid_jpeg_accepted(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200


def test_upload_valid_png_accepted(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    png = _png_b64()
    with _blob_patches():
        response = client.post(
            "/profilepageupload",
            json=_image_body(png, prefix="data:image/png;base64,"),
        )
    assert response.status_code == 200


def test_upload_gif_rejected_415(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post(
        "/profilepageupload",
        json=_image_body(_GIF, prefix="data:image/gif;base64,"),
    )
    assert response.status_code == 415
    assert response.json()["detail"] == "UNSUPPORTED_PROFILE_IMAGE_TYPE"


def test_upload_webp_rejected_415(db_session):
    webp = _webp_b64()
    if webp is None:
        pytest.importorskip("PIL.WebPImagePlugin")
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post(
        "/profilepageupload",
        json=_image_body(webp, prefix="data:image/webp;base64,"),
    )
    assert response.status_code == 415


def test_upload_malformed_base64_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post(
        "/profilepageupload",
        json={"image": "data:image/jpeg;base64,%%%not-base64!!!"},
    )
    assert response.status_code == 422


def test_upload_empty_image_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post("/profilepageupload", json={"image": "   "})
    assert response.status_code == 422


def test_upload_mime_content_mismatch_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.post(
        "/profilepageupload",
        json=_image_body(_jpeg_b64(), prefix="data:image/png;base64,"),
    )
    assert response.status_code == 422


def test_upload_corrupt_jpeg_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    corrupt = base64.b64encode(b"\xff\xd8\xff\x00notajpeg").decode()
    response = client.post(
        "/profilepageupload",
        json=_image_body(corrupt),
    )
    assert response.status_code == 422


def test_upload_corrupt_png_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    corrupt = base64.b64encode(b"\x89PNG\r\n\x1a\nnotapng").decode()
    response = client.post(
        "/profilepageupload",
        json=_image_body(corrupt, prefix="data:image/png;base64,"),
    )
    assert response.status_code == 422


def test_exactly_at_limit_size_check_allows_equal():
    at_limit = b"x" * user_crud._MAX_PROFILE_IMAGE_BYTES
    with pytest.raises(HTTPException) as exc:
        user_crud._validate_profile_image_bytes(at_limit, None)
    assert exc.value.status_code != 413


def test_decoded_over_2mb_413(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    over = base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
    response = client.post("/profilepageupload", json=_image_body(over))
    assert response.status_code == 413
    assert response.json()["detail"] == "PROFILE_IMAGE_TOO_LARGE"


def test_encoded_payload_over_2mb_413(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    over = base64.b64encode(b"y" * (2 * 1024 * 1024 + 512)).decode()
    response = client.post("/profilepageupload", json={"image": over})
    assert response.status_code == 413


def test_upload_decompression_bomb_422(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with patch.object(user_crud, "_PROFILE_MAX_IMAGE_PIXELS", 1):
        with _blob_patches():
            response = client.post(
                "/profilepageupload",
                json=_image_body(_png_b64(), prefix="data:image/png;base64,"),
            )
    assert response.status_code == 422


def test_upload_raw_base64_without_prefix_accepted(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post(
            "/profilepageupload",
            json={"image": _jpeg_b64()},
        )
    assert response.status_code == 200


# --- Storage / DB -----------------------------------------------------------


def test_upload_uses_jwt_owned_blob_name(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches() as (upload_calls, _):
        client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert upload_calls[0]["blob_name"] == f"{CUSTOMER_ID}_profile"


def test_upload_make_public_true(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches() as (upload_calls, _):
        client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert upload_calls[0]["make_public"] is True


def test_upload_message_uploaded_and_public_url(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    body = response.json()
    assert body["message"] == "UPLOADED"
    assert body["url"].startswith("https://example.blob.core.windows.net/")


def test_upload_cache_buster_present(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert "v=" in response.json()["url"]


def test_second_upload_changes_version_query(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    stamps = iter([1000, 2000])

    with _blob_patches(), patch.object(user_crud, "time") as time_mock:
        time_mock.time.side_effect = lambda: next(stamps) / 1000.0
        r1 = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
        r2 = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert r1.json()["url"] != r2.json()["url"]
    assert "v=1000" in r1.json()["url"]
    assert "v=2000" in r2.json()["url"]


def test_upload_only_profile_picture_updated(db_session):
    db, engine, Session = db_session
    before_ts = datetime(2024, 6, 1, 10, 0, 0)
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        fullName="Keep Name",
        emailId="keep@example.com",
        dob="1988-02-02",
        city="Keep City",
        gender="Female",
        alsoVendor=False,
        vendorApproved=False,
        rating="3.3",
        totalNoOfReviews=9,
        customerRating="4.1",
        totalCustomerReviews=7,
        tableTimestamp=before_ts,
    )
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200
    db.expire_all()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert user.profilePicture.startswith("https://example.blob.core.windows.net/")
    assert user.fullName == "Keep Name"
    assert user.emailId == "keep@example.com"
    assert user.dob == "1988-02-02"
    assert user.city == "Keep City"
    assert user.gender == "Female"
    assert user.alsoVendor is False
    assert user.vendorApproved is False
    assert user.rating == "3.3"
    assert user.totalNoOfReviews == 9
    assert user.customerRating == "4.1"
    assert user.totalCustomerReviews == 7
    assert user.tableTimestamp != before_ts


def test_storage_failure_no_db_mutation(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        profilePicture="images/original.png",
    )
    client = _pr23_client(engine, Session, CUSTOMER_ID)

    def _fail_upload(*args, **kwargs):
        return False, "INVALID"

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fail_upload):
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 422
    db.expire_all()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert user.profilePicture == "images/original.png"


def test_db_failure_after_upload_rollback_and_cleanup(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    delete_calls: list[str] = []

    def _fake_upload(blob_name, base64_data, make_public=False, max_upload_bytes=2097152):
        return True, f"https://example.blob.core.windows.net/container/{blob_name}.jpg"

    def _fake_delete(url):
        delete_calls.append(str(url))

    original_commit = db.commit

    def _boom_commit():
        raise SQLAlchemyError("commit failed")

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "azure_blob_delete_by_url", side_effect=_fake_delete
    ), patch.object(db, "commit", side_effect=_boom_commit):
        with pytest.raises(HTTPException) as exc:
            user_crud.profile_image_upload(
                db,
                UserImageUpload(image=f"data:image/jpeg;base64,{_jpeg_b64()}"),
                user_id=CUSTOMER_ID,
            )
    assert exc.value.status_code == 500
    assert exc.value.detail == "PROFILE_UPLOAD_FAILED"
    assert any("7022359323_profile" in url for url in delete_calls)
    db.expire_all()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert user.profilePicture == "images/profilepic_male.png"
    # restore commit for fixture teardown
    db.commit = original_commit


def test_old_different_path_blob_cleanup(db_session):
    db, engine, Session = db_session
    old_url = "https://example.blob.core.windows.net/container/legacy/old.jpg"
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, profilePicture=old_url)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches() as (_, delete_calls):
        client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert old_url in delete_calls


def test_same_path_overwrite_does_not_delete_current_blob(db_session):
    db, engine, Session = db_session
    same_base = f"https://example.blob.core.windows.net/container/{CUSTOMER_ID}_profile.jpg"
    old_url = f"{same_base}?v=111"
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, profilePicture=old_url)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches() as (_, delete_calls):
        client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert old_url not in delete_calls
    assert same_base not in delete_calls


def test_cleanup_failure_does_not_undo_committed_profile_update(db_session):
    db, engine, Session = db_session
    old_url = "https://example.blob.core.windows.net/container/legacy/old.jpg"
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, profilePicture=old_url)
    client = _pr23_client(engine, Session, CUSTOMER_ID)

    def _raise_delete(url):
        raise RuntimeError("delete failed")

    with _blob_patches(), patch.object(
        user_crud, "azure_blob_delete_by_url", side_effect=_raise_delete
    ):
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 200
    db.expire_all()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert user.profilePicture.startswith("https://example.blob.core.windows.net/")


def test_no_raw_exception_leakage_in_response(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)

    def _boom(*args, **kwargs):
        raise RuntimeError("super secret internal trace")

    with patch.object(user_crud, "azure_blob_upload", side_effect=_boom):
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    assert response.status_code == 500
    assert response.json()["detail"] == "PROFILE_UPLOAD_FAILED"
    assert "super secret" not in response.text
    assert "trace" not in response.text.lower()


def test_no_storage_credentials_in_response(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    _assert_no_sensitive_response(response.json(), response.text)


def test_no_kyc_bank_fields_in_upload_response(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        response = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    body = response.json()
    assert set(body.keys()) == {"message", "url"}
    _assert_no_sensitive_response(body, response.text)


def test_crud_does_not_close_db_session(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)

    def _fake_upload(blob_name, base64_data, make_public=False, max_upload_bytes=2097152):
        return True, f"https://example.blob.core.windows.net/container/{blob_name}.jpg"

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload):
        user_crud.profile_image_upload(
            db,
            UserImageUpload(image=f"data:image/jpeg;base64,{_jpeg_b64()}"),
            user_id=CUSTOMER_ID,
        )
    row = db.query(User).filter(User.userAppId == CUSTOMER_ID).one()
    assert "7022359323_profile" in row.profilePicture


# --- GET /getuserdetails ownership ------------------------------------------


def test_getuserdetails_own_user_succeeds(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_getuserdetails_matching_user_app_id_succeeds(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID})
    assert response.status_code == 200
    assert response.json()[0]["USERAPPID"] == CUSTOMER_ID


def test_getuserdetails_mismatched_user_app_id_403(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails", params={"userAppId": OTHER_USER})
    assert response.status_code == 403


def test_getuserdetails_pr6_response_fields_present(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    row = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID}).json()[0]
    for key in (
        "USERAPPID",
        "FULLNAME",
        "EMAILID",
        "EMAIL",
        "DOB",
        "CITY",
        "GENDER",
        "PROFILEPIC",
        "CUSTOMERRATING",
        "TOTALCUSTOMERRATING",
        "ALSOVENDOR",
        "VENDOR",
    ):
        assert key in row


def test_getuserdetails_new_profilepic_after_upload(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with _blob_patches():
        upload = client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    uploaded_url = upload.json()["url"]
    row = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID}).json()[0]
    assert row["PROFILEPIC"] == uploaded_url


def test_getuserdetails_missing_user_soft_no_registered(db_session):
    _, engine, Session = db_session
    client = _pr23_client(engine, Session, MISSING_USER)
    response = client.get("/getuserdetails", params={"userAppId": MISSING_USER})
    assert response.status_code == 200
    assert response.json() == {"message": "NO REGISTERED"}


def test_getuserdetails_compatible_list_shape(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    response = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID})
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert "password" not in row
    assert "bankAccountNo" not in row
    assert "imageAadhar" not in row
    assert "123456789012" not in response.text


def test_getuserdetails_no_jwt_returns_401_or_403(db_session):
    _, engine, Session = db_session
    client = _pr23_client(engine, Session, user_id=None)
    response = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID})
    assert response.status_code in (401, 403)


def test_sensitive_values_not_logged_on_upload(db_session, caplog):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr23_client(engine, Session, CUSTOMER_ID)
    with caplog.at_level(logging.DEBUG), _blob_patches():
        client.post("/profilepageupload", json=_image_body(_jpeg_b64()))
    joined = " ".join(r.message for r in caplog.records)
    assert "secret-fcm-token" not in joined
    assert "123456789012" not in joined
    assert "unit-test-jwt-secret" not in joined
