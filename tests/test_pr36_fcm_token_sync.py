"""
PR36 — JWT-owned FCM token registration hardening.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
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
os.environ.setdefault("DB_USERNAME", "unused")
os.environ.setdefault("DB_PASSWORD", "unused")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "3306")
os.environ.setdefault("DB_NAME", "unused")
os.environ["RATE_LIMIT_FCM_TOKEN_UPDATE_PER_USER"] = "10"
os.environ["RATE_LIMIT_FCM_TOKEN_UPDATE_WINDOW_SECONDS"] = "3600"

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base, get_db  # noqa: E402
from app_v1.auth.deps import AuthenticatedUser, get_current_user, get_current_user_id  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.endpoints.user import router as user_router  # noqa: E402
from app_v1.utils.security import hash_password  # noqa: E402

CUSTOMER_ID = "7022359323"
VENDOR_ID = "8637554388"
PENDING_VENDOR = "8637554387"
LOCKED_USER = "7000000001"
OTHER_USER = "7000000002"
MISSING_USER = "7999999999"
PASSWORD = "SecretPass1"
TOKEN_A = "fcm-token-alpha-001"
TOKEN_B = "fcm-token-beta-002"

PR36_TABLES = [
    User.__table__,
    ApiRateLimitBucket.__table__,
]


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=PR36_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _client(engine, Session, user_id: str | None):
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
    else:
        def _unauthorized():
            raise HTTPException(status_code=401, detail="Could not validate credentials")

        app.dependency_overrides[get_current_user_id] = _unauthorized
        app.dependency_overrides[get_current_user] = _unauthorized
    return TestClient(app, raise_server_exceptions=False)


def _add_user(db, *, user_app_id: str, uid: int, **kwargs):
    user = User(
        UID=uid,
        userAppId=user_app_id,
        password=kwargs.pop("password", hash_password(PASSWORD)),
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
        fcmToken=kwargs.pop("fcmToken", None),
        user_login_status=kwargs.pop("user_login_status", "LOGGEDIN"),
        tableTimestamp=kwargs.pop("tableTimestamp", datetime(2024, 1, 1, 12, 0, 0)),
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _put(client, body=None, query: str = ""):
    path = f"/fcmtokenupdate{query}"
    return client.put(path, json=body if body is not None else {"fcmToken": TOKEN_A})


# ---------------------------------------------------------------------------
# Auth / ownership
# ---------------------------------------------------------------------------



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

def test_01_missing_jwt():
    engine, Session = _memory_db()
    client = _client(engine, Session, user_id=None)
    resp = _put(client)
    assert resp.status_code == 401


def test_02_invalid_jwt_dependency():
    engine, Session = _memory_db()
    client = _client(engine, Session, user_id=None)
    resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 401


def test_03_jwt_user_missing():
    engine, Session = _memory_db()
    client = _client(engine, Session, user_id=MISSING_USER)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"


def test_04_tombstoned_user_rejected():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=f"{CUSTOMER_ID}.DELETED", uid=1, fcmToken="old")
    db.close()
    client = _client(engine, Session, user_id=f"{CUSTOMER_ID}.DELETED")
    resp = _put(client)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "USER_NOT_FOUND"
    db = Session()
    row = db.query(User).filter(User.UID == 1).first()
    assert row.fcmToken == "old"
    db.close()


def test_05_locked_live_user_allowed():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=LOCKED_USER, uid=1, lockApp=True, fcmToken=None)
    db.close()
    client = _client(engine, Session, user_id=LOCKED_USER)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ) as sub:
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200
    assert resp.json() == {"message": "UPDATED"}
    assert sub.called
    db = Session()
    assert db.query(User).filter(User.userAppId == LOCKED_USER).first().fcmToken == TOKEN_A
    db.close()


def test_06_customer_allowed():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200
    assert resp.json()["message"] == "UPDATED"


def test_07_pending_vendor_allowed():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=PENDING_VENDOR,
        uid=1,
        alsoVendor=True,
        vendorApproved=False,
    )
    db.close()
    client = _client(engine, Session, user_id=PENDING_VENDOR)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200


def test_08_approved_vendor_allowed():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=VENDOR_ID,
        uid=1,
        alsoVendor=True,
        vendorApproved=True,
    )
    db.close()
    client = _client(engine, Session, user_id=VENDOR_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200


def test_09_10_jwt_subject_owns_update_cannot_update_other():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="mine")
    _add_user(db, user_app_id=OTHER_USER, uid=2, fcmToken="theirs")
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        # Legacy query identity must not redirect ownership.
        resp = client.put(
            f"/fcmtokenupdate?userAppId={OTHER_USER}&fcmToken=evil",
            json={"fcmToken": TOKEN_B},
        )
    assert resp.status_code == 200
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first().fcmToken == TOKEN_B
    assert db.query(User).filter(User.userAppId == OTHER_USER).first().fcmToken == "theirs"
    db.close()


def test_11_query_user_app_id_not_required():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = client.put("/fcmtokenupdate", json={"fcmToken": TOKEN_A})
    assert resp.status_code == 200


def test_12_query_fcm_token_not_accepted_as_contract():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="keep-me")
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    # Query-only (no body) must fail validation — body is required.
    resp = client.put(f"/fcmtokenupdate?fcmToken={TOKEN_A}")
    assert resp.status_code == 422
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first().fcmToken == "keep-me"
    db.close()


# ---------------------------------------------------------------------------
# Happy path / idempotency / timestamp
# ---------------------------------------------------------------------------


def test_13_valid_json_token_update():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200
    assert resp.json() == {"message": "UPDATED"}


def test_14_15_same_value_replay_no_timestamp_churn():
    engine, Session = _memory_db()
    db = Session()
    ts = datetime(2024, 6, 1, 10, 0, 0)
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken=TOKEN_A, tableTimestamp=ts)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ) as sub:
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200
    assert resp.json()["message"] == "UPDATED"
    assert sub.called is False
    db = Session()
    row = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert row.fcmToken == TOKEN_A
    assert row.tableTimestamp == ts
    db.close()


def test_16_changed_value_does_not_bump_table_timestamp():
    engine, Session = _memory_db()
    db = Session()
    ts = datetime(2024, 6, 1, 10, 0, 0)
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken=TOKEN_A, tableTimestamp=ts)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client, {"fcmToken": TOKEN_B})
    assert resp.status_code == 200
    db = Session()
    row = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert row.fcmToken == TOKEN_B
    assert row.tableTimestamp == ts
    db.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "null",
        "NULL",
        "none",
        "None",
        "na",
        "NA",
        "tok\x00en",
        "tok en",
        "a" * 4097,
    ],
)
def test_17_21_invalid_tokens_rejected(bad):
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="keep")
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    resp = _put(client, {"fcmToken": bad})
    assert resp.status_code == 422
    assert "keep" not in resp.text or bad != "keep"
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first().fcmToken == "keep"
    db.close()


def test_22_extra_body_field_rejected():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    resp = _put(client, {"fcmToken": TOKEN_A, "userAppId": CUSTOMER_ID})
    assert resp.status_code == 422


def test_23_no_token_in_response():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert TOKEN_A not in resp.text
    assert "fcmToken" not in resp.text


def test_24_no_provider_sql_leak_on_db_failure():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)

    class Boom(SQLAlchemyError):
        pass

    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        with patch(
            "sqlalchemy.orm.Session.commit",
            side_effect=Boom("SECRET_SQL_DETAIL xyz"),
        ):
            # Force changed-token path: empty stored token
            resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "FCM_TOKEN_UPDATE_FAILED"
    assert "SECRET_SQL" not in resp.text
    assert TOKEN_A not in resp.text


def test_25_db_rollback_on_failure():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken="before")
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)

    class Boom(SQLAlchemyError):
        pass

    with patch(
        "sqlalchemy.orm.Session.commit",
        side_effect=Boom("fail"),
    ):
        resp = _put(client, {"fcmToken": TOKEN_B})
    assert resp.status_code == 500
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first().fcmToken == "before"
    db.close()


def test_26_topic_subscription_after_commit():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=VENDOR_ID, uid=1, alsoVendor=True, vendorApproved=True)
    db.close()
    client = _client(engine, Session, user_id=VENDOR_ID)
    order = []

    def _commit_side_effect(self):
        order.append("commit")
        return object.__getattribute__(
            type(self).__mro__[1] if False else self, "__class__"
        )

    real_commit = None

    from sqlalchemy.orm import Session as SASession

    original_commit = SASession.commit

    def tracking_commit(self):
        order.append("commit")
        return original_commit(self)

    with patch.object(SASession, "commit", tracking_commit):
        with patch(
            "app_v1.crud.user.subscribe_token_to_topics",
            side_effect=lambda *a, **k: (order.append("subscribe"), {"success": True})[1],
        ):
            resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200
    assert "commit" in order
    assert "subscribe" in order
    assert order.index("commit") < order.index("subscribe")


def test_27_topic_failure_does_not_undo_updated():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": False, "error": "PROVIDER_SECRET"},
    ):
        resp = _put(client, {"fcmToken": TOKEN_A})
    assert resp.status_code == 200
    assert resp.json() == {"message": "UPDATED"}
    assert "PROVIDER_SECRET" not in resp.text
    db = Session()
    assert db.query(User).filter(User.userAppId == CUSTOMER_ID).first().fcmToken == TOKEN_A
    db.close()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_28_changed_token_rate_limit():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken=None)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        for i in range(10):
            resp = _put(client, {"fcmToken": f"token-changed-{i}"})
            assert resp.status_code == 200, resp.text
        resp = _put(client, {"fcmToken": "token-changed-overflow"})
    assert resp.status_code == 429
    assert resp.json()["detail"] == "FCM_TOKEN_UPDATE_RATE_LIMITED"


def test_29_same_value_replay_does_not_consume_rate_limit():
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken=TOKEN_A)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        for _ in range(15):
            resp = _put(client, {"fcmToken": TOKEN_A})
            assert resp.status_code == 200
        # First changed write after idempotent replays still allowed.
        resp = _put(client, {"fcmToken": TOKEN_B})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Logout / deletion / helpers / OpenAPI / X-Client-Id
# ---------------------------------------------------------------------------


def test_30_31_logout_clears_token_and_unsubscribes_previous():
    engine, Session = _memory_db()
    db = Session()
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        fcmToken=TOKEN_A,
        alsoVendor=True,
    )
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.unsubscribe_token_from_topics",
        return_value={},
    ) as unsub:
        resp = client.post(
            f"/logout?userAppId={CUSTOMER_ID}&fcmToken=client-ignored-token",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "LOGGEDOUT" or body.get("messsage") == "LOGOUT_SUCCESS"
    db = Session()
    row = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert row.fcmToken is None
    db.close()
    assert unsub.called
    args, kwargs = unsub.call_args
    assert TOKEN_A in (args[0] if args else kwargs.get("token", ""))
    assert "client-ignored-token" not in str(unsub.call_args)


def test_32_account_deletion_still_clears_token():
    from app_v1.models.request_table import Request
    from app_v1.models.car_details import CarDetail
    from app_v1.models.driver_details import DriverDetail
    from sqlalchemy import text

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            Request.__table__,
            CarDetail.__table__,
            DriverDetail.__table__,
            ApiRateLimitBucket.__table__,
        ],
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS biddetails (
                    BID INTEGER PRIMARY KEY,
                    rID INTEGER NOT NULL,
                    bidderID INTEGER NOT NULL,
                    CARID INTEGER,
                    bidAmount NUMERIC(11,2) NOT NULL,
                    bidStatus VARCHAR(100),
                    tableTimestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    _add_user(
        db,
        user_app_id=CUSTOMER_ID,
        uid=1,
        fcmToken=TOKEN_A,
        password=hash_password(PASSWORD),
    )
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch("app_v1.crud.user.unsubscribe_token_from_topics", return_value={}):
        resp = client.post(
            "/deleteappuser",
            json={
                "password": PASSWORD,
                "deletionReason": "User requested account deletion",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "DELETED"
    db = Session()
    rows = db.query(User).all()
    assert all((r.fcmToken is None) for r in rows)
    assert any(".DELETED" in (r.userAppId or "").upper() for r in rows)
    db.close()


def test_33_notification_helpers_read_updated_token():
    """Helpers read User.fcmToken from DB; after PR36 update the column is current."""
    engine, Session = _memory_db()
    db = Session()
    user = _add_user(db, user_app_id=CUSTOMER_ID, uid=1, fcmToken=None)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        assert _put(client, {"fcmToken": TOKEN_A}).status_code == 200
    db = Session()
    row = db.query(User).filter(User.userAppId == CUSTOMER_ID).first()
    assert row.fcmToken == TOKEN_A
    # Mimic notifications._clean_token read path
    from app_v1.services.notifications import _clean_token

    assert _clean_token(row.fcmToken) == TOKEN_A
    db.close()


def test_34_x_client_id_optional_unchanged():
    """Dependency still accepts requests when X-Client-Id is absent (override path)."""
    engine, Session = _memory_db()
    db = Session()
    _add_user(db, user_app_id=CUSTOMER_ID, uid=1)
    db.close()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    with patch(
        "app_v1.crud.user.subscribe_token_to_topics",
        return_value={"success": True},
    ):
        resp = client.put(
            "/fcmtokenupdate",
            json={"fcmToken": TOKEN_A},
            headers={},
        )
    assert resp.status_code == 200


def test_35_openapi_body_contract_not_query_token():
    engine, Session = _memory_db()
    client = _client(engine, Session, user_id=CUSTOMER_ID)
    schema = client.app.openapi()
    path = schema["paths"]["/fcmtokenupdate"]["put"]
    params = path.get("parameters") or []
    param_names = {p.get("name") for p in params if isinstance(p, dict)}
    assert "fcmToken" not in param_names
    assert "userAppId" not in param_names
    body = path["requestBody"]["content"]["application/json"]["schema"]
    # Resolve $ref if present
    if "$ref" in body:
        ref = body["$ref"].split("/")[-1]
        body = schema["components"]["schemas"][ref]
    props = body.get("properties") or {}
    assert "fcmToken" in props
    assert body.get("additionalProperties") is False or "userAppId" not in props
