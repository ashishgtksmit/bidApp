"""
PR16 vendor onboarding / KYC — JWT-owned PUT /registernewvendor.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import base64
import os
import sys
import types
from datetime import date
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

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.crud import user as user_crud  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402

CUSTOMER_ID = "7022359323"
PENDING_VENDOR = "8637554387"
APPROVED_VENDOR = "8637554388"
LOCKED_USER = "7000000001"
OTHER_USER = "7000000002"

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")

PR16_TABLES = [User.__table__]


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR16_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _pr16_client(engine, Session, user_id: str | None):
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
        app.dependency_overrides[get_current_user] = lambda: _pr38_auth_user(user_id)
    return TestClient(app)


def _add_user(db, *, user_app_id: str, uid: int, **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password="secret",
        alternateNumber="1000000000",
        fullName=kwargs.get("fullName", "User"),
        emailId=f"{user_app_id}@example.com",
        dob=kwargs.get("dob", "1990-01-01"),
        city=kwargs.get("city", "Gangtok"),
        gender=kwargs.get("gender", "Male"),
        profilePicture=kwargs.get("profilePicture", "images/profilepic_male.png"),
        alsoVendor=kwargs.get("alsoVendor", False),
        vendorApproved=kwargs.get("vendorApproved", False),
        lockApp=kwargs.get("lockApp", False),
        customerRating="4.5",
        totalCustomerReviews=0,
        rating=kwargs.get("rating", "4.5"),
        totalNoOfReviews=kwargs.get("totalNoOfReviews", 3),
        fcmToken=kwargs.get("fcmToken", "secret-fcm-token-should-not-leak"),
        joiningDate=kwargs.get("joiningDate", None),
        tags=kwargs.get("tags", None),
        noOfTripsCompleted=kwargs.get("noOfTripsCompleted", 12),
        user_login_status="LOGGEDOUT",
        cityPreferences=kwargs.get("cityPreferences", "1"),
        requestTypePreferences=kwargs.get("requestTypePreferences", None),
        regionPreferences=kwargs.get("regionPreferences", None),
        address=kwargs.get("address", None),
        state=kwargs.get("state", None),
        bankAccountHolderName=kwargs.get("bankAccountHolderName", None),
        bankAccountNo=kwargs.get("bankAccountNo", None),
        bankIFSC=kwargs.get("bankIFSC", None),
        bankName=kwargs.get("bankName", None),
        imageAadhar=kwargs.get("imageAadhar", None),
        imagePAN=kwargs.get("imagePAN", None),
        imageBankAccount=kwargs.get("imageBankAccount", None),
    )
    db.add(user)
    db.commit()
    return user


def _valid_body(**overrides):
    body = {
        "dob": "1991-05-15",
        "gender": "Male",
        "addressLine1": "Line 1",
        "addressLine2": "Line 2",
        "city": "Gangtok",
        "state": "Sikkim",
        "bankAccountHolderName": "Test Holder",
        "bankAccountNo": "123456789012",
        "bankIFSC": "SBIN0001234",
        "bankName": "State Bank of India",
        "imageAadhar": _PNG_B64,
        "imagePAN": _PNG_B64,
        "imageBankAccount": _PNG_B64,
    }
    body.update(overrides)
    return body


def _fake_upload(blob_name, base64_data, make_public=False, max_upload_bytes=None):
    return True, f"https://example.blob.core.windows.net/vendor/{blob_name}.png"



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

@pytest.fixture
def db_session():
    engine, Session = _memory_db()
    db = Session()
    try:
        yield db, engine, Session
    finally:
        db.close()


def test_no_token_returns_401(db_session):
    _, engine, Session = db_session
    # Do not override get_current_user_id — real dependency requires JWT.
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
    response = client.put("/registernewvendor", json=_valid_body())
    # HTTPBearer(auto_error=True) returns 403 when Authorization is absent;
    # invalid/expired JWT paths return 401 from get_current_user_id.
    assert response.status_code in (401, 403)


def test_customer_only_submits_and_forces_also_vendor(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False, vendorApproved=False)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "azure_blob_delete_by_url", return_value=True
    ), patch.object(user_crud, "send_email", return_value={"message": "SENT"}) as email_mock:
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 200
    assert response.json() == {"message": "UPDATED", "error": None}
    assert "imageAadhar" not in response.text
    assert "https://example.blob" not in response.text

    db.expire_all()
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert user.alsoVendor is True
    assert user.vendorApproved is False
    assert user.lockApp is False
    assert user.dob == "1991-05-15"
    assert user.gender == "Male"
    assert user.bankIFSC == "SBIN0001234"
    assert user.bankAccountNo == "123456789012"
    assert user.requestTypePreferences == "1,2,3,4"
    assert user.joiningDate is not None
    assert "Aadhaar_" in user.imageAadhar
    assert email_mock.called
    email_kwargs = email_mock.call_args.kwargs
    assert email_kwargs["from_address"] == "customersupport@wizzride.com"
    assert "123456789012" not in email_kwargs["message"]
    assert "********9012" in email_kwargs["message"] or "9012" in email_kwargs["message"]


def test_client_user_app_id_absent_jwt_authoritative(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    body = _valid_body()
    assert "userAppId" not in body

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        response = client.put("/registernewvendor", json=body)

    assert response.status_code == 200
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert user.alsoVendor is True


def test_mismatching_legacy_identity_forbidden(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    _add_user(db, user_app_id=OTHER_USER, uid=2)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    response = client.put(
        "/registernewvendor",
        json=_valid_body(userAppId=OTHER_USER),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not authorized"
    user = db.query(User).filter(User.userAppId == OTHER_USER).first()
    assert user.alsoVendor is False


def test_missing_user_404(db_session):
    _, engine, Session = db_session
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload):
        response = client.put("/registernewvendor", json=_valid_body())
    assert response.status_code == 404
    assert response.json()["detail"] == "USER_NOT_FOUND"


def test_lock_app_account_locked(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=LOCKED_USER, uid=1, lockApp=True, alsoVendor=False)
    client = _pr16_client(engine, Session, LOCKED_USER)
    response = client.put("/registernewvendor", json=_valid_body())
    assert response.status_code == 403
    assert response.json()["detail"] == "ACCOUNT_LOCKED"


def test_approved_vendor_already_vendor(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=True,
        imageAadhar="https://old/aadhaar.png",
    )
    client = _pr16_client(engine, Session, APPROVED_VENDOR)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload) as upload_mock:
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 409
    assert response.json()["detail"] == "ALREADY_VENDOR"
    assert upload_mock.call_count == 0
    user = db.query(User).filter(User.userAppId == APPROVED_VENDOR).first()
    assert user.imageAadhar == "https://old/aadhaar.png"
    assert user.vendorApproved is True


def test_pending_vendor_may_resubmit_preserves_approval_false(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
        requestTypePreferences="2,3",
        joiningDate=date(2024, 1, 1),
        imageAadhar="https://old/aadhaar.png",
        imagePAN="https://old/pan.png",
        imageBankAccount="https://old/bank.png",
        fcmToken="keep-fcm",
        rating="4.8",
    )
    client = _pr16_client(engine, Session, PENDING_VENDOR)
    deleted = []

    def _delete(url):
        deleted.append(url)
        return True

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "azure_blob_delete_by_url", side_effect=_delete
    ), patch.object(user_crud, "send_email", return_value={"message": "SENT"}):
        response = client.put(
            "/registernewvendor",
            json=_valid_body(bankAccountNo="999988887777", bankIFSC="hdfc0004321"),
        )

    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    user = db.query(User).filter(User.userAppId == PENDING_VENDOR).first()
    assert user.alsoVendor is True
    assert user.vendorApproved is False
    assert user.requestTypePreferences == "2,3"
    assert user.joiningDate == date(2024, 1, 1)
    assert user.bankAccountNo == "999988887777"
    assert user.bankIFSC == "HDFC0004321"
    assert user.fcmToken == "keep-fcm"
    assert user.rating == "4.8"
    assert "https://old/aadhaar.png" in deleted
    assert "https://old/pan.png" in deleted
    assert "https://old/bank.png" in deleted


def test_first_name_last_name_not_required(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    body = _valid_body()
    assert "firstName" not in body
    assert "lastName" not in body
    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        response = client.put("/registernewvendor", json=body)
    assert response.status_code == 200


def test_datetime_timestamp_dob_rejected(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    response = client.put(
        "/registernewvendor",
        json=_valid_body(dob="1991-05-15 10:11:12.123456"),
    )
    assert response.status_code == 422


def test_invalid_ifsc(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    response = client.put("/registernewvendor", json=_valid_body(bankIFSC="BAD"))
    assert response.status_code == 422
    assert "ERROR_INVALID_IFSC" in str(response.json())


def test_invalid_account_number(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    response = client.put("/registernewvendor", json=_valid_body(bankAccountNo="12"))
    assert response.status_code == 422
    assert "ERROR_INVALID_ACCOUNTNO" in str(response.json())


def test_invalid_gender(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    response = client.put("/registernewvendor", json=_valid_body(gender="Unknown"))
    assert response.status_code == 422


def test_address_line2_required(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    body = _valid_body()
    del body["addressLine2"]
    response = client.put("/registernewvendor", json=body)
    assert response.status_code == 422


def test_client_cannot_set_also_vendor_false_or_approval_flags(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False, vendorApproved=False)
    client = _pr16_client(engine, Session, CUSTOMER_ID)
    body = _valid_body(alsoVendor=False, vendorApproved=True, lockApp=True)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        response = client.put("/registernewvendor", json=body)

    assert response.status_code == 200
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert user.alsoVendor is True
    assert user.vendorApproved is False
    assert user.lockApp is False


def test_empty_prefs_initialized_explicit_prefs_preserved(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, requestTypePreferences=None)
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=2,
        alsoVendor=True,
        vendorApproved=False,
        requestTypePreferences="4",
    )

    client_a = _pr16_client(engine, Session, CUSTOMER_ID)
    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        assert client_a.put("/registernewvendor", json=_valid_body()).status_code == 200
    user_a = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert user_a.requestTypePreferences == "1,2,3,4"

    client_b = _pr16_client(engine, Session, PENDING_VENDOR)
    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        assert client_b.put("/registernewvendor", json=_valid_body()).status_code == 200
    user_b = db.query(User).filter(User.userAppId == PENDING_VENDOR).first()
    assert user_b.requestTypePreferences == "4"


def test_invalid_base64_media(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    def _bad_upload(blob_name, base64_data, make_public=False, max_upload_bytes=None):
        return False, "INVALID_BASE64"

    with patch.object(user_crud, "azure_blob_upload", side_effect=_bad_upload):
        response = client.put("/registernewvendor", json=_valid_body())
    assert response.status_code == 422
    assert response.json()["detail"] == "ERROR_INVALID_MEDIA"
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert user.alsoVendor is False


def test_media_too_large(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    def _large_upload(blob_name, base64_data, make_public=False, max_upload_bytes=None):
        return False, "FILE_TOO_LARGE"

    with patch.object(user_crud, "azure_blob_upload", side_effect=_large_upload):
        response = client.put("/registernewvendor", json=_valid_body())
    assert response.status_code == 422
    assert response.json()["detail"] == "ERROR_MEDIA_TOO_LARGE"


def test_upload_failure_rollback_cleans_new_blobs(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
        imageAadhar="https://old/aadhaar.png",
    )
    client = _pr16_client(engine, Session, PENDING_VENDOR)
    deleted = []
    calls = {"n": 0}

    def _upload(blob_name, base64_data, make_public=False, max_upload_bytes=None):
        calls["n"] += 1
        if calls["n"] == 3:
            return False, "INVALID_IMAGE"
        return True, f"https://example.blob/{blob_name}.png"

    def _delete(url):
        deleted.append(url)
        return True

    with patch.object(user_crud, "azure_blob_upload", side_effect=_upload), patch.object(
        user_crud, "azure_blob_delete_by_url", side_effect=_delete
    ):
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 422
    user = db.query(User).filter(User.userAppId == PENDING_VENDOR).first()
    assert user.imageAadhar == "https://old/aadhaar.png"
    assert "https://old/aadhaar.png" not in deleted
    assert any("Aadhaar_" in u for u in deleted)
    assert any("PAN_" in u for u in deleted)


def test_email_failure_does_not_undo_commit(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", side_effect=RuntimeError("smtp down")
    ):
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 200
    assert response.json()["message"] == "UPDATED"
    user = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert user.alsoVendor is True


def test_approved_replay_sends_no_email(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=APPROVED_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=True,
    )
    client = _pr16_client(engine, Session, APPROVED_VENDOR)

    with patch.object(user_crud, "send_email", return_value={"message": "SENT"}) as email_mock:
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 409
    assert email_mock.call_count == 0


def test_getuserdetails_also_vendor_true_after_commit(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        assert client.put("/registernewvendor", json=_valid_body()).status_code == 200

    details = client.get("/getuserdetails", params={"userAppId": CUSTOMER_ID})
    assert details.status_code == 200
    row = details.json()[0]
    assert row["ALSOVENDOR"] is True
    assert row["VENDOR"] is True
    assert "imageAadhar" not in row
    assert "bankAccountNo" not in row


def test_admin_approval_race_blocks_overwrite(db_session):
    db, engine, Session = db_session
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
        imageAadhar="https://old/aadhaar.png",
    )
    client = _pr16_client(engine, Session, PENDING_VENDOR)
    calls = {"n": 0}
    deleted = []

    def _upload_then_approve(blob_name, base64_data, make_public=False, max_upload_bytes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            race_db = Session()
            try:
                u = race_db.query(User).filter(User.userAppId == PENDING_VENDOR).first()
                u.vendorApproved = True
                race_db.commit()
            finally:
                race_db.close()
        return True, f"https://example.blob/{blob_name}.png"

    def _delete(url):
        deleted.append(url)
        return True

    with patch.object(user_crud, "azure_blob_upload", side_effect=_upload_then_approve), patch.object(
        user_crud, "azure_blob_delete_by_url", side_effect=_delete
    ), patch.object(user_crud, "send_email", return_value={"message": "SENT"}):
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 409
    assert response.json()["detail"] == "ALREADY_VENDOR"
    db.expire_all()
    user = db.query(User).filter(User.userAppId == PENDING_VENDOR).first()
    assert user.vendorApproved is True
    assert user.imageAadhar == "https://old/aadhaar.png"
    assert any("Aadhaar_" in u or "PAN_" in u or "Bank_" in u for u in deleted)


def test_kyc_email_from_env_override(db_session, monkeypatch):
    monkeypatch.setenv("KYC_EMAIL_FROM", "reservations@wizzride.com")
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ) as email_mock:
        response = client.put("/registernewvendor", json=_valid_body())

    assert response.status_code == 200
    assert email_mock.call_args.kwargs["from_address"] == "reservations@wizzride.com"


def test_success_body_contains_no_kyc_urls_or_sql(db_session):
    db, engine, Session = db_session
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    client = _pr16_client(engine, Session, CUSTOMER_ID)

    with patch.object(user_crud, "azure_blob_upload", side_effect=_fake_upload), patch.object(
        user_crud, "send_email", return_value={"message": "SENT"}
    ):
        response = client.put("/registernewvendor", json=_valid_body())

    payload = response.json()
    assert payload == {"message": "UPDATED", "error": None}
    assert "SELECT" not in response.text
    assert "blob" not in response.text.lower()
