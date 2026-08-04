"""PR38 — Immutable JWT subject (authSubjectId) + dual-identity compatibility."""

from __future__ import annotations

import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "pr38-test-secret")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_ISSUER", "openbid-backend")
os.environ.setdefault("JWT_AUDIENCE", "openbid-frontend")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "30")
os.environ.setdefault("JWT_CLOCK_LEEWAY_SECONDS", "30")
os.environ.setdefault("JWT_ALLOW_LEGACY_PHONE_SUB", "true")
os.environ.setdefault("JWT_IDENTITY_VERSION_TO_MINT", "2")

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.auth import jwt as jwt_mod  # noqa: E402
from app_v1.auth.deps import (  # noqa: E402
    AuthenticatedUser,
    get_current_user,
    get_current_user_id,
    hash_auth_subject_for_limit,
    resolve_token_user,
    validate_access_session,
)
from app_v1.auth.jwt import (  # noqa: E402
    IDENTITY_VERSION_IMMUTABLE,
    IDENTITY_VERSION_LEGACY_PHONE,
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_SECRET,
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
)
from app_v1.database import Base, get_db  # noqa: E402
from app_v1.endpoints import auth as auth_ep  # noqa: E402
from app_v1.endpoints import user as user_ep  # noqa: E402
from app_v1.models.client_secrets import ClientSecret  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket, PasswordResetToken  # noqa: E402
from app_v1.models.user_table import (  # noqa: E402
    User,
    _new_account_session_id,
    _new_auth_subject_id,
)
from app_v1.utils.security import hash_password  # noqa: E402
from app_v1.crud.auth import update_password  # noqa: E402

PHONE = "7022359323"
PASSWORD = "TestPass1!"

PR38_TABLES = [
    User.__table__,
    ApiRateLimitBucket.__table__,
    ClientSecret.__table__,
    PasswordResetToken.__table__,
]


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR38_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_user(
    db,
    *,
    user_app_id: str = PHONE,
    uid: int = 1,
    password: str = PASSWORD,
    session_version: int = 1,
    account_session_id: Optional[str] = None,
    auth_subject_id: Optional[str] = None,
    **kwargs,
):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password=password,
        fullName=kwargs.pop("fullName", "Test User"),
        emailId=kwargs.pop("emailId", f"{user_app_id}@example.com"),
        dob=kwargs.pop("dob", "1990-01-01"),
        city=kwargs.pop("city", "Guwahati"),
        gender=kwargs.pop("gender", "Male"),
        alsoVendor=kwargs.pop("alsoVendor", False),
        vendorApproved=kwargs.pop("vendorApproved", False),
        lockApp=kwargs.pop("lockApp", False),
        rating=kwargs.pop("rating", "5"),
        totalNoOfReviews=kwargs.pop("totalNoOfReviews", 0),
        sessionVersion=session_version,
        accountSessionId=account_session_id or _new_account_session_id(),
        authSubjectId=auth_subject_id or _new_auth_subject_id(),
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _client(db):
    app = FastAPI()
    app.include_router(auth_ep.router)
    app.include_router(user_ep.router)

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=False)


def _decode_unverified(token: str) -> dict:
    return jwt.get_unverified_claims(token)


def _mint_v2_pair(db, user: User, *, client_id: Optional[str] = None):
    access = create_access_token(
        db=db,
        auth_subject=str(user.authSubjectId),
        identity_version=IDENTITY_VERSION_IMMUTABLE,
        session_version=int(user.sessionVersion),
        account_session_id=str(user.accountSessionId),
        client_id=client_id,
        roles=["vendor"] if user.alsoVendor else ["user"],
    )
    refresh = create_refresh_token(
        db=db,
        auth_subject=str(user.authSubjectId),
        identity_version=IDENTITY_VERSION_IMMUTABLE,
        session_version=int(user.sessionVersion),
        account_session_id=str(user.accountSessionId),
        client_id=client_id,
    )
    return access, refresh


def _mint_pr37_token(
    db,
    user: User,
    *,
    token_type: str = "refresh",
    include_identity_version: bool = False,
    subject: Optional[str] = None,
    session_version: Optional[int] = None,
    account_session_id: Optional[str] = None,
    omit_claims: Optional[set] = None,
    exp_delta: Optional[timedelta] = None,
):
    """Mint PR37 phone-sub token (compat path)."""
    now = datetime.now(timezone.utc)
    exp = now + (exp_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    payload = {
        "sub": subject if subject is not None else user.userAppId,
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
        "session_version": session_version
        if session_version is not None
        else int(user.sessionVersion),
        "session_id": account_session_id
        if account_session_id is not None
        else str(user.accountSessionId),
    }
    if include_identity_version:
        payload["identity_version"] = IDENTITY_VERSION_LEGACY_PHONE
    if omit_claims:
        for key in omit_claims:
            payload.pop(key, None)
    if token_type == "access":
        payload["roles"] = ["vendor"] if user.alsoVendor else ["user"]
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Model / migration
# ---------------------------------------------------------------------------


def test_model_auth_subject_unique_and_opaque(db_session):
    a = _add_user(db_session, uid=1, user_app_id="1111111111")
    b = _add_user(db_session, uid=2, user_app_id="2222222222")
    assert a.authSubjectId
    assert b.authSubjectId
    assert a.authSubjectId != b.authSubjectId
    assert a.authSubjectId != a.userAppId
    assert a.authSubjectId != str(a.UID)
    assert a.authSubjectId != a.accountSessionId
    assert len(a.authSubjectId) >= 32


def test_tombstone_receives_and_retains_auth_subject(db_session):
    user = _add_user(db_session)
    original = user.authSubjectId
    user.userAppId = f"{PHONE}.DELETED"
    user.sessionVersion = int(user.sessionVersion) + 1
    db_session.commit()
    db_session.refresh(user)
    assert user.authSubjectId == original


def test_new_user_default_auth_subject(db_session):
    user = User(
        UID=9,
        userAppId="9999999999",
        password="x",
        fullName="N",
        emailId="n@example.com",
        dob="1990-01-01",
        city="X",
        alsoVendor=False,
        vendorApproved=False,
        lockApp=False,
        rating="5",
        totalNoOfReviews=0,
        sessionVersion=1,
        accountSessionId=_new_account_session_id(),
        # authSubjectId via column default
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    assert user.authSubjectId
    assert user.authSubjectId != user.userAppId


def test_migration_package_exists_and_does_not_claim_production():
    mig = ROOT / "migrations" / "pr38_immutable_auth_subject"
    assert (mig / "apply_migration.py").is_file()
    assert (mig / "preflight_immutable_auth_subject.py").is_file()
    assert (mig / "audit_immutable_auth_subject.py").is_file()
    readme = (mig / "README.md").read_text()
    assert "authSubjectId" in readme
    assert "claimed" in readme.lower()
    assert "production" in readme.lower()


# ---------------------------------------------------------------------------
# Login / mint claims
# ---------------------------------------------------------------------------


def test_login_mints_v2_opaque_pair(db_session):
    _add_user(db_session, password=hash_password(PASSWORD))
    client = _client(db_session)
    resp = client.post(
        "/login",
        json={"userAppId": PHONE, "password": PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    access = _decode_unverified(body["access_token"])
    refresh = _decode_unverified(body["refresh_token"])
    # Re-query — login_user_auth closes the request session in production code.
    user = db_session.query(User).filter(User.userAppId == PHONE).first()
    assert user is not None
    for claims in (access, refresh):
        assert claims["sub"] == user.authSubjectId
        assert claims["identity_version"] == 2
        assert claims["sub"] != PHONE
        assert "phone" not in claims
        assert "userAppId" not in claims
        assert "user_app_id" not in claims
        assert "UID" not in claims
        assert "uid" not in claims
        assert claims["session_version"] == user.sessionVersion
        assert claims["session_id"] == user.accountSessionId
    assert access["identity_version"] == refresh["identity_version"]
    assert access["roles"] == ["user"]
    assert "authSubjectId" not in str(body.get("user"))


# ---------------------------------------------------------------------------
# Refresh compatibility
# ---------------------------------------------------------------------------


def test_v2_refresh_stays_v2(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_v2_pair(db_session, user)
    resp = _client(db_session).post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    access = _decode_unverified(resp.json()["access_token"])
    new_r = _decode_unverified(resp.json()["refresh_token"])
    assert access["sub"] == user.authSubjectId
    assert access["identity_version"] == 2
    assert new_r["identity_version"] == 2


def test_pr37_refresh_converts_to_v2(db_session):
    user = _add_user(db_session)
    legacy = _mint_pr37_token(db_session, user, token_type="refresh")
    resp = _client(db_session).post("/refresh", json={"refresh_token": legacy})
    assert resp.status_code == 200
    access = _decode_unverified(resp.json()["access_token"])
    assert access["sub"] == user.authSubjectId
    assert access["identity_version"] == 2
    assert access["sub"] != user.userAppId


def test_pr37_refresh_explicit_identity_v1_converts(db_session):
    user = _add_user(db_session)
    legacy = _mint_pr37_token(
        db_session, user, token_type="refresh", include_identity_version=True
    )
    resp = _client(db_session).post("/refresh", json={"refresh_token": legacy})
    assert resp.status_code == 200
    assert _decode_unverified(resp.json()["access_token"])["identity_version"] == 2


def test_pr37_session_mismatch_rejected(db_session):
    user = _add_user(db_session, session_version=2)
    bad_sv = _mint_pr37_token(
        db_session, user, token_type="refresh", session_version=1
    )
    bad_sid = _mint_pr37_token(
        db_session,
        user,
        token_type="refresh",
        account_session_id="wrong-session-id",
    )
    client = _client(db_session)
    assert client.post("/refresh", json={"refresh_token": bad_sv}).status_code == 401
    assert client.post("/refresh", json={"refresh_token": bad_sid}).status_code == 401


def test_pr37_phone_reuse_rejected(db_session):
    old = _add_user(db_session, uid=1)
    legacy = _mint_pr37_token(db_session, old, token_type="refresh")
    old.userAppId = f"{PHONE}.DELETED"
    old.sessionVersion = int(old.sessionVersion) + 1
    db_session.commit()
    _add_user(db_session, uid=2, user_app_id=PHONE)
    resp = _client(db_session).post("/refresh", json={"refresh_token": legacy})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_claimless_refresh_rejected(db_session):
    user = _add_user(db_session)
    token = _mint_pr37_token(
        db_session,
        user,
        token_type="refresh",
        omit_claims={"session_version", "session_id"},
    )
    resp = _client(db_session).post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401


def test_legacy_refresh_rejected_when_compat_disabled(db_session):
    user = _add_user(db_session)
    legacy = _mint_pr37_token(db_session, user, token_type="refresh")
    with patch("app_v1.auth.deps.allow_legacy_phone_sub", return_value=False):
        resp = _client(db_session).post(
            "/refresh", json={"refresh_token": legacy}
        )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_unknown_identity_version_rejected(db_session):
    user = _add_user(db_session)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.authSubjectId,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(days=1)).timestamp()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
        "session_version": int(user.sessionVersion),
        "session_id": str(user.accountSessionId),
        "identity_version": 99,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    resp = _client(db_session).post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401


def test_identity_version_downgrade_rejected(db_session):
    """v2 auth subject presented as identity_version=1 must not phone-resolve."""
    user = _add_user(db_session)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.authSubjectId,  # not a phone
        "type": "refresh",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(days=1)).timestamp()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
        "session_version": int(user.sessionVersion),
        "session_id": str(user.accountSessionId),
        "identity_version": 1,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    with patch("app_v1.auth.deps.allow_legacy_phone_sub", return_value=True):
        resp = _client(db_session).post(
            "/refresh", json={"refresh_token": token}
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Access / deps
# ---------------------------------------------------------------------------


def test_v2_access_resolves_authenticated_user(db_session):
    user = _add_user(db_session, alsoVendor=True)
    access, _ = _mint_v2_pair(db_session, user)
    payload = jwt_mod.decode_token(db=db_session, token=access, client_id=None)
    auth = validate_access_session(db_session, payload=payload)
    assert isinstance(auth, AuthenticatedUser)
    assert auth.uid == user.UID
    assert auth.auth_subject == user.authSubjectId
    assert auth.user_app_id == user.userAppId
    assert auth.identity_version == 2
    assert auth.session_version == user.sessionVersion
    assert "vendor" in auth.roles


def test_pr37_access_allowed_during_compat(db_session):
    user = _add_user(db_session)
    access = _mint_pr37_token(db_session, user, token_type="access")
    payload = jwt_mod.decode_token(db=db_session, token=access, client_id=None)
    auth = resolve_token_user(
        db_session, payload, expected_type="access", allow_legacy=True
    )
    assert auth.user_app_id == user.userAppId
    assert auth.identity_version == 1


def test_pr37_access_rejected_when_compat_disabled(db_session):
    user = _add_user(db_session)
    access = _mint_pr37_token(db_session, user, token_type="access")
    payload = jwt_mod.decode_token(db=db_session, token=access, client_id=None)
    with patch("app_v1.auth.deps.allow_legacy_phone_sub", return_value=False):
        with pytest.raises(Exception):
            resolve_token_user(
                db_session, payload, expected_type="access", allow_legacy=False
            )


def test_raw_v2_sub_not_treated_as_phone(db_session):
    user = _add_user(db_session)
    access, _ = _mint_v2_pair(db_session, user)
    payload = jwt_mod.decode_token(db=db_session, token=access, client_id=None)
    auth = validate_access_session(db_session, payload=payload)
    assert auth.user_app_id == PHONE
    assert auth.auth_subject != PHONE
    assert payload["sub"] != PHONE


def test_tombstoned_user_access_rejected(db_session):
    user = _add_user(db_session)
    access, _ = _mint_v2_pair(db_session, user)
    user.userAppId = f"{PHONE}.DELETED"
    user.sessionVersion = int(user.sessionVersion) + 1
    db_session.commit()
    payload = jwt_mod.decode_token(db=db_session, token=access, client_id=None)
    with pytest.raises(Exception):
        validate_access_session(db_session, payload=payload)


def test_password_reset_revokes_v2_tokens(db_session):
    user = _add_user(db_session)
    access, refresh = _mint_v2_pair(db_session, user)
    # Direct sessionVersion bump simulates successful password reset
    user.sessionVersion = int(user.sessionVersion) + 1
    db_session.commit()
    payload = jwt_mod.decode_token(db=db_session, token=access, client_id=None)
    with pytest.raises(Exception):
        validate_access_session(db_session, payload=payload)
    resp = _client(db_session).post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Endpoints: getuserdetails / logout / ws-validate
# ---------------------------------------------------------------------------


def test_getuserdetails_queryless_and_mismatch(db_session):
    user = _add_user(db_session)
    access, _ = _mint_v2_pair(db_session, user)
    client = _client(db_session)
    headers = {"Authorization": f"Bearer {access}"}
    ok = client.get("/getuserdetails", headers=headers)
    assert ok.status_code == 200
    assert ok.json()[0]["USERAPPID"] == PHONE

    match = client.get(
        "/getuserdetails",
        headers=headers,
        params={"userAppId": PHONE},
    )
    assert match.status_code == 200

    bad = client.get(
        "/getuserdetails",
        headers=headers,
        params={"userAppId": "0000000000"},
    )
    assert bad.status_code == 403


def test_logout_targets_jwt_user_not_query_phone(db_session):
    user = _add_user(db_session, fcmToken="token-a")
    other = _add_user(
        db_session,
        uid=2,
        user_app_id="9999999999",
        fcmToken="token-b",
    )
    access, _ = _mint_v2_pair(db_session, user)
    client = _client(db_session)
    # Attempt to point query at other phone — ownership is JWT; mismatch → 403
    resp = client.post(
        "/logout",
        headers={"Authorization": f"Bearer {access}"},
        params={"userAppId": other.userAppId, "fcmToken": "x"},
    )
    assert resp.status_code == 403

    resp2 = client.post(
        "/logout",
        headers={"Authorization": f"Bearer {access}"},
        params={"fcmToken": "hint"},
    )
    assert resp2.status_code == 200
    db_session.refresh(user)
    db_session.refresh(other)
    assert user.fcmToken is None
    assert other.fcmToken == "token-b"


def test_ws_validate_returns_phone_appid(db_session):
    user = _add_user(db_session)
    access, _ = _mint_v2_pair(db_session, user)
    resp = _client(db_session).post(
        "/ws-validate",
        json={
            "token": access,
            "client_id": "flutter-android",
            "flag": "Customer",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["appid"] == PHONE
    assert body["flag"] == "Customer"
    assert isinstance(body["exp"], int)


def test_ws_validate_pr37_access_during_compat(db_session):
    user = _add_user(db_session)
    access = _mint_pr37_token(db_session, user, token_type="access")
    resp = _client(db_session).post(
        "/ws-validate",
        json={
            "token": access,
            "client_id": "flutter-android",
            "flag": "Customer",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["appid"] == PHONE


def test_fcm_jwt_owned(db_session):
    user = _add_user(db_session)
    access, _ = _mint_v2_pair(db_session, user)
    resp = _client(db_session).put(
        "/fcmtokenupdate",
        headers={"Authorization": f"Bearer {access}"},
        json={"fcmToken": "new-fcm-token"},
    )
    assert resp.status_code == 200
    db_session.refresh(user)
    assert user.fcmToken == "new-fcm-token"


# ---------------------------------------------------------------------------
# Security / source guards
# ---------------------------------------------------------------------------


def test_hash_auth_subject_for_limit_not_raw():
    raw = "opaque-auth-subject-value"
    hashed = hash_auth_subject_for_limit(raw)
    assert hashed != raw
    assert len(hashed) == 32


def test_source_guards_jwt_and_deps():
    jwt_src = (ROOT / "app_v1" / "auth" / "jwt.py").read_text()
    deps_src = (ROOT / "app_v1" / "auth" / "deps.py").read_text()
    auth_crud = (ROOT / "app_v1" / "crud" / "auth.py").read_text()
    user_ep = (ROOT / "app_v1" / "endpoints" / "user.py").read_text()
    env = (ROOT / ".env.example").read_text()

    assert "auth_subject" in jwt_src
    assert "identity_version" in jwt_src
    assert "JWT_ALLOW_LEGACY_PHONE_SUB" in env
    assert "JWT_IDENTITY_VERSION_TO_MINT" in env
    assert "class AuthenticatedUser" in deps_src
    assert "def get_current_user(" in deps_src
    assert "Deprecated" in deps_src or "deprecated" in deps_src
    assert "ensure_auth_subject_id" in auth_crud
    assert "IDENTITY_VERSION_IMMUTABLE" in auth_crud
    assert "Optional[str] = Query(" in user_ep  # queryless getuserdetails
    assert "userAppId : str = Query(...)" not in user_ep.replace(" ", "")
    # no refresh family / per-device sessions in auth layer
    assert "refresh_token_family" not in auth_crud.lower()
    assert "device_session" not in auth_crud.lower()


def test_mint_helpers_reject_missing_auth_subject(db_session):
    user = _add_user(db_session)
    with pytest.raises(ValueError):
        create_access_token(
            db=db_session,
            auth_subject="",
            identity_version=2,
            session_version=1,
            account_session_id=user.accountSessionId,
            client_id=None,
        )


def test_get_current_user_id_returns_phone_not_raw_sub(db_session):
    """Deprecated wrapper returns user_app_id for v2 tokens."""
    from fastapi import Depends

    user = _add_user(db_session)
    access, _ = _mint_v2_pair(db_session, user)
    app = FastAPI()

    @app.get("/whoami-phone")
    def whoami(phone: str = Depends(get_current_user_id)):
        return {"phone": phone}

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/whoami-phone", headers={"Authorization": f"Bearer {access}"}
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] == PHONE
    assert resp.json()["phone"] != user.authSubjectId
