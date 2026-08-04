"""PR37 — Refresh token lifecycle hardening.

Covers refresh-token-only contract, session identity, expiry units,
password-reset/deletion revocation, rate limits, and X-Client-Id signing.
"""

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

# Ensure JWT env before importing app modules
os.environ.setdefault("JWT_SECRET", "pr37-test-secret")
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
from app_v1.auth.deps import get_current_user_id  # noqa: E402
from app_v1.auth.jwt import (  # noqa: E402
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_SECRET,
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app_v1.database import Base, get_db  # noqa: E402
from app_v1.endpoints import auth as auth_ep  # noqa: E402
from app_v1.models.client_secrets import ClientSecret  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket, PasswordResetToken  # noqa: E402
from app_v1.models.user_table import User, _new_account_session_id  # noqa: E402
from app_v1.utils.security import hash_password  # noqa: E402
from app_v1.crud.user import logout_user  # noqa: E402
from app_v1.crud.auth import update_password  # noqa: E402

PHONE = "7022359323"
PASSWORD = "TestPass1!"
CLIENT_ID = "flutter-android"
CLIENT_SECRET = "client-secret-pr37"

PR37_TABLES = [
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
    Base.metadata.create_all(bind=engine, tables=PR37_TABLES)
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
        authSubjectId=kwargs.pop("authSubjectId", None) or __import__("uuid").uuid4().hex,
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _client(db, *, include_user_router: bool = False):
    app = FastAPI()
    app.include_router(auth_ep.router)

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=False)


def _decode_unverified(token: str) -> dict:
    return jwt.get_unverified_claims(token)


def _mint_refresh(
    db,
    user: User,
    *,
    client_id: Optional[str] = None,
    session_version: Optional[int] = None,
    account_session_id: Optional[str] = None,
    subject: Optional[str] = None,
    extra: Optional[dict] = None,
    exp_delta: Optional[timedelta] = None,
    nbf_delta: Optional[timedelta] = None,
    secret: Optional[str] = None,
    omit_claims: Optional[set] = None,
    token_type: str = "refresh",
):
    """Low-level mint for negative tests (bypasses helpers when needed)."""
    now = datetime.now(timezone.utc)
    exp = now + (exp_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    nbf = now + (nbf_delta or timedelta(0))
    payload = {
        "sub": subject if subject is not None else user.userAppId,
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(nbf.timestamp()),
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
    if extra:
        payload.update(extra)
    if omit_claims:
        for key in omit_claims:
            payload.pop(key, None)
    return jwt.encode(
        payload,
        secret or JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def _mint_pair(db, user: User, *, client_id: Optional[str] = None):
    access = create_access_token(
        db=db,
        auth_subject=str(user.authSubjectId),
        identity_version=2,
        session_version=int(user.sessionVersion),
        account_session_id=str(user.accountSessionId),
        client_id=client_id,
        roles=["vendor"] if user.alsoVendor else ["user"],
    )
    refresh = create_refresh_token(
        db=db,
        auth_subject=str(user.authSubjectId),
        identity_version=2,
        session_version=int(user.sessionVersion),
        account_session_id=str(user.accountSessionId),
        client_id=client_id,
    )
    return access, refresh


# ---------------------------------------------------------------------------
# Refresh contract
# ---------------------------------------------------------------------------


def test_01_no_authorization_valid_refresh_succeeds(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_02_expired_access_header_valid_refresh(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    expired_access = _mint_refresh(
        db_session,
        user,
        token_type="access",
        exp_delta=timedelta(minutes=-10),
    )
    client = _client(db_session)
    resp = client.post(
        "/refresh",
        json={"refresh_token": refresh},
        headers={"Authorization": f"Bearer {expired_access}"},
    )
    assert resp.status_code == 200


def test_03_invalid_access_header_valid_refresh(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    client = _client(db_session)
    resp = client.post(
        "/refresh",
        json={"refresh_token": refresh},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 200


def test_04_valid_access_header_ignored_for_ownership(db_session):
    user_a = _add_user(db_session, user_app_id="1111111111", uid=1)
    user_b = _add_user(db_session, user_app_id="2222222222", uid=2)
    access_a, _ = _mint_pair(db_session, user_a)
    _, refresh_b = _mint_pair(db_session, user_b)
    client = _client(db_session)
    resp = client.post(
        "/refresh",
        json={"refresh_token": refresh_b},
        headers={"Authorization": f"Bearer {access_a}"},
    )
    assert resp.status_code == 200
    claims = _decode_unverified(resp.json()["access_token"])
    assert claims["sub"] == user_b.authSubjectId
    assert claims["identity_version"] == 2
    assert claims["sub"] != user_b.userAppId


def test_05_missing_refresh_body(db_session):
    client = _client(db_session)
    resp = client.post("/refresh", json={})
    assert resp.status_code == 422


def test_06_malformed_refresh_token(db_session):
    _add_user(db_session)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": "not.a.jwt"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_07_invalid_signature(db_session):
    user = _add_user(db_session)
    bad = _mint_refresh(db_session, user, secret="wrong-secret")
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": bad})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_08_wrong_issuer(db_session):
    user = _add_user(db_session)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.userAppId,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(days=1)).timestamp()),
        "iss": "evil-issuer",
        "aud": JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
        "session_version": 1,
        "session_id": user.accountSessionId,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_09_wrong_audience(db_session):
    user = _add_user(db_session)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.userAppId,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(days=1)).timestamp()),
        "iss": JWT_ISSUER,
        "aud": "wrong-aud",
        "jti": uuid.uuid4().hex,
        "session_version": 1,
        "session_id": user.accountSessionId,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_10_access_token_used_as_refresh(db_session):
    user = _add_user(db_session)
    access, _ = _mint_pair(db_session, user)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": access})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_11_missing_type(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(db_session, user, omit_claims={"type"})
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_12_wrong_type(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(db_session, user, token_type="access")
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_13_missing_sub(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(db_session, user, omit_claims={"sub"})
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_14_missing_session_version(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(db_session, user, omit_claims={"session_version"})
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_15_missing_session_id(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(db_session, user, omit_claims={"session_id"})
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_16_expired_refresh(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(
        db_session, user, exp_delta=timedelta(minutes=-5)
    )
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "REFRESH_TOKEN_EXPIRED"


def test_17_nbf_outside_leeway(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(
        db_session, user, nbf_delta=timedelta(seconds=120)
    )
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REFRESH_TOKEN"


def test_18_leeway_accepted(db_session):
    user = _add_user(db_session)
    # nbf 20s in the future — within 30s leeway
    token = _mint_refresh(
        db_session, user, nbf_delta=timedelta(seconds=20)
    )
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 200


def test_19_beyond_leeway_rejected(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(
        db_session, user, nbf_delta=timedelta(seconds=60)
    )
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401


def test_20_missing_user_session_invalid(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(db_session, user)
    db_session.query(User).delete()
    db_session.commit()
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_21_tombstoned_user_session_invalid(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    user.userAppId = f"{PHONE}.DELETED"
    user.lockApp = True
    user.sessionVersion = int(user.sessionVersion) + 1
    db_session.commit()
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_22_locked_live_user_allowed(db_session):
    user = _add_user(db_session, lockApp=True)
    _, refresh = _mint_pair(db_session, user)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200


def test_23_customer_allowed(db_session):
    user = _add_user(db_session, alsoVendor=False)
    _, refresh = _mint_pair(db_session, user)
    assert _client(db_session).post(
        "/refresh", json={"refresh_token": refresh}
    ).status_code == 200


def test_24_pending_vendor_allowed(db_session):
    user = _add_user(db_session, alsoVendor=True, vendorApproved=False)
    _, refresh = _mint_pair(db_session, user)
    assert _client(db_session).post(
        "/refresh", json={"refresh_token": refresh}
    ).status_code == 200


def test_25_approved_vendor_allowed(db_session):
    user = _add_user(db_session, alsoVendor=True, vendorApproved=True)
    _, refresh = _mint_pair(db_session, user)
    assert _client(db_session).post(
        "/refresh", json={"refresh_token": refresh}
    ).status_code == 200


def test_26_session_version_mismatch(db_session):
    user = _add_user(db_session, session_version=2)
    token = _mint_refresh(db_session, user, session_version=1)
    resp = _client(db_session).post(
        "/refresh", json={"refresh_token": token}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_27_account_session_id_mismatch(db_session):
    user = _add_user(db_session)
    token = _mint_refresh(
        db_session, user, account_session_id=uuid.uuid4().hex
    )
    resp = _client(db_session).post(
        "/refresh", json={"refresh_token": token}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_28_old_phone_token_cannot_auth_recreated_account(db_session):
    old = _add_user(db_session, uid=1)
    _, old_refresh = _mint_pair(db_session, old)
    old.userAppId = f"{PHONE}.DELETED"
    old.lockApp = True
    old.sessionVersion = int(old.sessionVersion) + 1
    db_session.commit()

    new = _add_user(db_session, uid=2, user_app_id=PHONE)
    assert new.accountSessionId != old.accountSessionId

    resp = _client(db_session).post(
        "/refresh", json={"refresh_token": old_refresh}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "SESSION_INVALID"


def test_29_new_account_unique_account_session_id(db_session):
    a = _add_user(db_session, uid=1, user_app_id="1111111111")
    b = _add_user(db_session, uid=2, user_app_id="2222222222")
    assert a.accountSessionId
    assert b.accountSessionId
    assert a.accountSessionId != b.accountSessionId


def test_30_two_accounts_different_session_ids(db_session):
    test_29_new_account_unique_account_session_id(db_session)


def test_31_32_login_tokens_contain_claims(db_session):
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
    for claims in (access, refresh):
        assert claims["sub"] == db_session.query(User).filter(User.userAppId == PHONE).first().authSubjectId
        assert claims["identity_version"] == 2
        assert claims["sub"] != PHONE
        assert "session_version" in claims
        assert "session_id" in claims
        assert "jti" in claims
        assert claims["iss"] == JWT_ISSUER
        assert claims["aud"] == JWT_AUDIENCE
    assert access["type"] == "access"
    assert refresh["type"] == "refresh"
    assert access["roles"] == ["user"]


def test_33_34_refresh_issued_tokens_contain_claims(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    resp = _client(db_session).post(
        "/refresh", json={"refresh_token": refresh}
    )
    assert resp.status_code == 200
    body = resp.json()
    access = _decode_unverified(body["access_token"])
    new_refresh = _decode_unverified(body["refresh_token"])
    for claims in (access, new_refresh):
        assert claims["session_version"] == user.sessionVersion
        assert claims["session_id"] == user.accountSessionId
        assert "jti" in claims


def test_35_jti_differs(db_session):
    user = _add_user(db_session)
    a1, r1 = _mint_pair(db_session, user)
    a2, r2 = _mint_pair(db_session, user)
    jtis = {
        _decode_unverified(a1)["jti"],
        _decode_unverified(r1)["jti"],
        _decode_unverified(a2)["jti"],
        _decode_unverified(r2)["jti"],
    }
    assert len(jtis) == 4


def test_36_refresh_access_includes_current_roles(db_session):
    user = _add_user(db_session, alsoVendor=True, vendorApproved=True)
    _, refresh = _mint_pair(db_session, user)
    # Stale roles must not be copied from refresh — helper refresh has no roles
    resp = _client(db_session).post(
        "/refresh", json={"refresh_token": refresh}
    )
    access = _decode_unverified(resp.json()["access_token"])
    assert access["roles"] == ["vendor"]


def test_37_38_39_password_reset_revokes(db_session):
    user = _add_user(db_session)
    access, refresh = _mint_pair(db_session, user)
    # Same transaction semantics as update_password (password + sessionVersion)
    user.password = "newpass"
    user.sessionVersion = int(user.sessionVersion) + 1
    db_session.commit()
    assert int(user.sessionVersion) == 2

    client = _client(db_session)
    r = client.post("/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401
    assert r.json()["detail"] == "SESSION_INVALID"

    app = FastAPI()

    @app.get("/probe")
    def probe(uid: str = __import__("fastapi").Depends(get_current_user_id)):
        return {"uid": uid}

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.get("/probe", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 401


def test_37b_update_password_increments_session_version(db_session):
    _add_user(db_session)
    with patch("app_v1.utils.otp.consume_reset_token", return_value=None):
        # Avoid finally db.close() killing the fixture session
        with patch.object(db_session, "close"):
            result = update_password(
                db_session,
                user_app_id=PHONE,
                password="brand-new",
                reset_token="tok",
            )
    assert result.message == "UPDATED"
    db_session.expire_all()
    user = db_session.query(User).filter(User.userAppId == PHONE).first()
    assert int(user.sessionVersion) == 2
    assert user.password == "brand-new"


def test_40_41_42_deletion_revokes(db_session):
    user = _add_user(db_session)
    access, refresh = _mint_pair(db_session, user)
    # Simulate deletion transaction fields
    user.userAppId = f"{PHONE}.DELETED"
    user.lockApp = True
    user.sessionVersion = int(user.sessionVersion) + 1
    db_session.commit()

    client = _client(db_session)
    r = client.post("/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401
    assert r.json()["detail"] == "SESSION_INVALID"

    app = FastAPI()

    @app.get("/probe")
    def probe(uid: str = __import__("fastapi").Depends(get_current_user_id)):
        return {"uid": uid}

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.get("/probe", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 401


def test_43_new_account_same_phone_rejects_old(db_session):
    test_28_old_phone_token_cannot_auth_recreated_account(db_session)


def test_44_45_logout_does_not_increment_version(db_session):
    user = _add_user(db_session)
    sv = int(user.sessionVersion)
    _, refresh = _mint_pair(db_session, user)
    logout_user(db_session, PHONE, fcm_token=None)
    db_session.expire_all()
    user2 = db_session.query(User).filter(User.userAppId == PHONE).first()
    assert int(user2.sessionVersion) == sv
    # Residual risk: refresh still valid after normal logout
    resp = _client(db_session).post(
        "/refresh", json={"refresh_token": refresh}
    )
    assert resp.status_code == 200


def test_46_47_expiry_units(db_session):
    user = _add_user(db_session)
    now = datetime.now(timezone.utc)
    access = create_access_token(
        db=db_session,
        auth_subject=user.authSubjectId,
        identity_version=2,
        session_version=1,
        account_session_id=user.accountSessionId,
        client_id=None,
    )
    refresh = create_refresh_token(
        db=db_session,
        auth_subject=user.authSubjectId,
        identity_version=2,
        session_version=1,
        account_session_id=user.accountSessionId,
        client_id=None,
    )
    a_exp = datetime.fromtimestamp(
        _decode_unverified(access)["exp"], tz=timezone.utc
    )
    r_exp = datetime.fromtimestamp(
        _decode_unverified(refresh)["exp"], tz=timezone.utc
    )
    access_delta = (a_exp - now).total_seconds()
    refresh_delta = (r_exp - now).total_seconds()
    # access ~ 15 minutes
    assert 14 * 60 < access_delta < 16 * 60
    # refresh ~ 30 days (NOT 30 minutes)
    assert 29 * 24 * 3600 < refresh_delta < 31 * 24 * 3600
    assert ACCESS_TOKEN_EXPIRE_MINUTES == 15
    assert REFRESH_TOKEN_EXPIRE_DAYS == 30


def test_48_49_50_rotation_no_reuse_detection(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    new_refresh = resp.json()["refresh_token"]
    assert new_refresh != refresh
    # Previous refresh still valid (no family reuse detection)
    resp2 = client.post("/refresh", json={"refresh_token": refresh})
    assert resp2.status_code == 200


def test_51_52_rate_limits(db_session):
    user = _add_user(db_session)
    client = _client(db_session)
    with patch.dict(
        os.environ,
        {
            "RATE_LIMIT_REFRESH_VALID_PER_SESSION": "2",
            "RATE_LIMIT_REFRESH_VALID_WINDOW_SECONDS": "3600",
            "RATE_LIMIT_REFRESH_INVALID_PER_BUCKET": "2",
            "RATE_LIMIT_REFRESH_INVALID_WINDOW_SECONDS": "900",
        },
    ):
        # Valid session limit
        for _ in range(2):
            _, refresh = _mint_pair(db_session, user)
            assert (
                client.post(
                    "/refresh", json={"refresh_token": refresh}
                ).status_code
                == 200
            )
        _, refresh = _mint_pair(db_session, user)
        limited = client.post("/refresh", json={"refresh_token": refresh})
        assert limited.status_code == 429
        assert limited.json()["detail"] == "REFRESH_RATE_LIMITED"

        # Invalid bucket
        for _ in range(2):
            bad = client.post(
                "/refresh", json={"refresh_token": "bad.token.value"}
            )
            assert bad.status_code == 401
        bad3 = client.post(
            "/refresh", json={"refresh_token": "bad.token.value"}
        )
        assert bad3.status_code == 429


def test_53_no_raw_token_in_rate_limit_key(db_session):
    user = _add_user(db_session)
    refresh = "raw-refresh-secret-value-should-not-appear"
    client = _client(db_session)
    client.post("/refresh", json={"refresh_token": refresh})
    keys = [r.bucket_key for r in db_session.query(ApiRateLimitBucket).all()]
    for key in keys:
        assert "raw-refresh-secret-value" not in key
        assert refresh not in key


def test_54_55_safe_500_and_rollback(db_session):
    user = _add_user(db_session)
    _, refresh = _mint_pair(db_session, user)
    client = _client(db_session)
    with patch(
        "app_v1.crud.auth.create_access_token",
        side_effect=RuntimeError("boom"),
    ):
        resp = client.post("/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "REFRESH_FAILED"
    assert "boom" not in resp.text
    assert "RuntimeError" not in resp.text


def test_56_no_soft_200_auth_errors(db_session):
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": "x"})
    assert resp.status_code != 200


def test_57_no_sql_jwt_leaks(db_session):
    client = _client(db_session)
    resp = client.post("/refresh", json={"refresh_token": "x.y.z"})
    text = resp.text.lower()
    assert "traceback" not in text
    assert "sqlalchemy" not in text
    assert "jose" not in text


def test_58_openapi_refresh_no_bearer_required(db_session):
    client = _client(db_session)
    schema = client.get("/openapi.json").json()
    refresh = schema["paths"]["/refresh"]["post"]
    security = refresh.get("security")
    # Either absent or empty — must not require BearerAuth
    if security:
        assert security == [] or security == [{}]
    # Parameterized deps should not inject HTTPBearer
    components = schema.get("components", {}).get("securitySchemes", {})
    # Route itself should not list Bearer as required
    assert "BearerAuth" not in str(refresh.get("security", []))


def test_59_60_x_client_id_signing(db_session):
    user = _add_user(db_session)
    db_session.add(
        ClientSecret(
            clientId=CLIENT_ID,
            clientName="flutter-android",
            secretKey=CLIENT_SECRET,
            isActive=True,
        )
    )
    db_session.commit()

    refresh = create_refresh_token(
        db=db_session,
        auth_subject=user.authSubjectId,
        identity_version=2,
        session_version=1,
        account_session_id=user.accountSessionId,
        client_id=CLIENT_ID,
    )
    client = _client(db_session)
    ok = client.post(
        "/refresh",
        json={"refresh_token": refresh},
        headers={"X-Client-Id": CLIENT_ID},
    )
    assert ok.status_code == 200

    # Wrong secret context (missing client id → JWT_SECRET) fails
    bad = client.post(
        "/refresh",
        json={"refresh_token": refresh},
    )
    assert bad.status_code == 401


def test_61_old_client_authorization_tolerated(db_session):
    test_02_expired_access_header_valid_refresh(db_session)


def test_62_login_regression(db_session):
    _add_user(db_session, password=hash_password(PASSWORD))
    resp = _client(db_session).post(
        "/login",
        json={"userAppId": PHONE, "password": PASSWORD, "fcmToken": "t"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_63_ws_validate_regression(db_session):
    user = _add_user(db_session)
    access, _ = _mint_pair(db_session, user)
    resp = _client(db_session).post(
        "/ws-validate",
        json={"token": access, "flag": "Customer", "client_id": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["appid"] == PHONE


def test_64_source_guard_refresh_no_get_current_user_id():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "app_v1",
        "endpoints",
        "auth.py",
    )
    src = open(path, encoding="utf-8").read()
    # Refresh endpoint must not depend on get_current_user_id
    assert "def refresh_token_endpoint" in src
    # Extract refresh function block roughly
    start = src.index("def refresh_token_endpoint")
    end = src.index("@router.post", start + 1) if "@router.post" in src[start + 1 :] else start + 800
    block = src[start:end]
    assert "get_current_user_id" not in block


def test_65_refresh_expiry_uses_days_in_source():
    path = os.path.join(
        os.path.dirname(__file__), "..", "app_v1", "auth", "jwt.py"
    )
    src = open(path, encoding="utf-8").read()
    assert "timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)" in src
    assert "timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)" in src


def test_66_password_reset_and_deletion_increment_in_source():
    auth_crud = open(
        os.path.join(
            os.path.dirname(__file__), "..", "app_v1", "crud", "auth.py"
        ),
        encoding="utf-8",
    ).read()
    user_crud = open(
        os.path.join(
            os.path.dirname(__file__), "..", "app_v1", "crud", "user.py"
        ),
        encoding="utf-8",
    ).read()
    assert "sessionVersion = current_sv + 1" in auth_crud
    assert "sessionVersion = int(user.sessionVersion or 1) + 1" in user_crud
    # logout must not increment
    logout_block_start = user_crud.index("def logout_user")
    logout_block = user_crud[logout_block_start : logout_block_start + 1200]
    assert "sessionVersion" not in logout_block
