"""
PR5 OTP + password-reset token contract tests.

Uses in-memory SQLite so tests do not require the production MySQL instance.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure bidApp root is importable when running pytest from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("OTP_PEPPER", "unit-test-otp-pepper")
os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ["OTP_TEST_BYPASS_SMS"] = "1"
os.environ["OTP_TEST_FIXED_OTP"] = "1234"
os.environ.setdefault("OTP_EXPIRY_MINUTES", "5")
os.environ.setdefault("OTP_MAX_ATTEMPTS", "5")
os.environ.setdefault("RESET_TOKEN_EXPIRY_MINUTES", "10")

from app_v1.database import Base  # noqa: E402
from app_v1.models.otp_challenge import (  # noqa: E402
    ApiRateLimitBucket,
    OtpChallenge,
    PasswordResetToken,
)
from app_v1.models.user_table import User  # noqa: E402
from app_v1.schemas.user_table import OtpVerifyResponse  # noqa: E402
from app_v1.utils.common import ErrorResponse  # noqa: E402
from app_v1.utils.otp import (  # noqa: E402
    consume_reset_token,
    hash_otp,
    send_otp_to_user,
    upsert_otp_challenge,
    verify_otp_for_user,
)
from app_v1.crud.auth import update_password  # noqa: E402


def _check_user_message(db, user_app_id: str) -> str:
    """Local stand-in for check_user without importing heavy crud.user deps."""
    users = db.query(User).filter(User.userAppId == user_app_id).first()
    if not users:
        return "NO USERS PRESENT"
    return "REGISTERED USER"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # SQLite needs only the PR5 + user tables for these tests.
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            OtpChallenge.__table__,
            PasswordResetToken.__table__,
            ApiRateLimitBucket.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_user(db, user_app_id: str = "7022359323", password: str = "oldpass"):
    user = User(
        UID=1,
        userAppId=user_app_id,
        password=password,
        alternateNumber="8637554387",
        fullName="Test User",
        emailId="test@example.com",
        dob="1990-01-01",
        city="Gangtok",
        gender="Male",
        alsoVendor=False,
        vendorApproved=False,
        lockApp=False,
        rating="5",
        totalNoOfReviews=0,
    )
    db.add(user)
    db.commit()
    return user


def test_check_registered_user_present(db):
    _add_user(db)
    assert _check_user_message(db, "7022359323") == "REGISTERED USER"


def test_check_registered_user_absent(db):
    assert _check_user_message(db, "9999999999") == "NO USERS PRESENT"


def test_otp_send_stores_hash_not_plaintext(db):
    result = send_otp_to_user(db, user_app_id="7022359323")
    assert isinstance(result, ErrorResponse)
    assert result.message == "OTP_SENT"

    row = db.query(OtpChallenge).filter(OtpChallenge.user_app_id == "7022359323").one()
    assert row.otp_hash == hash_otp("1234")
    assert row.otp_hash != "1234"
    assert row.attempt_count == 0


def test_verify_correct_otp_issues_reset_token(db):
    upsert_otp_challenge(db, "7022359323", "1234")
    result = verify_otp_for_user(db, "7022359323", "1234")
    assert isinstance(result, OtpVerifyResponse)
    assert result.message == "OTP_VERIFIED"
    assert result.reset_token
    assert db.query(OtpChallenge).filter(OtpChallenge.user_app_id == "7022359323").count() == 0
    assert db.query(PasswordResetToken).count() == 1


def test_verify_wrong_otp(db):
    upsert_otp_challenge(db, "7022359323", "1234")
    result = verify_otp_for_user(db, "7022359323", "9999")
    assert isinstance(result, ErrorResponse)
    assert result.message == "OTP_INVALID"


def test_verify_expired_otp(db):
    upsert_otp_challenge(db, "7022359323", "1234")
    row = db.query(OtpChallenge).one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.commit()

    result = verify_otp_for_user(db, "7022359323", "1234")
    assert isinstance(result, ErrorResponse)
    assert result.message == "OTP_EXPIRED"


def test_resend_invalidates_old_otp(db):
    upsert_otp_challenge(db, "7022359323", "1111")
    upsert_otp_challenge(db, "7022359323", "2222")

    old = verify_otp_for_user(db, "7022359323", "1111")
    assert isinstance(old, ErrorResponse)
    assert old.message == "OTP_INVALID"

    # Re-seed after failed attempt consumed attempt_count on current hash.
    upsert_otp_challenge(db, "7022359323", "2222")
    ok = verify_otp_for_user(db, "7022359323", "2222")
    assert isinstance(ok, OtpVerifyResponse)
    assert ok.message == "OTP_VERIFIED"


def test_max_attempts_locks(db):
    upsert_otp_challenge(db, "7022359323", "1234")
    for _ in range(5):
        result = verify_otp_for_user(db, "7022359323", "0000")
        assert isinstance(result, ErrorResponse)

    assert result.message == "OTP_LOCKED"
    # Challenge cleared after lock.
    assert db.query(OtpChallenge).filter(OtpChallenge.user_app_id == "7022359323").count() == 0


def test_reset_token_wrong_user_rejected(db):
    upsert_otp_challenge(db, "7022359323", "1234")
    verified = verify_otp_for_user(db, "7022359323", "1234")
    assert isinstance(verified, OtpVerifyResponse)

    err = consume_reset_token(db, "9999999999", verified.reset_token)
    assert err == "RESET_TOKEN_INVALID"


def test_reset_token_expired_rejected(db):
    upsert_otp_challenge(db, "7022359323", "1234")
    verified = verify_otp_for_user(db, "7022359323", "1234")
    row = db.query(PasswordResetToken).one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.commit()

    err = consume_reset_token(db, "7022359323", verified.reset_token)
    assert err == "RESET_TOKEN_EXPIRED"


def test_reset_token_reuse_rejected(db):
    _add_user(db)
    upsert_otp_challenge(db, "7022359323", "1234")
    verified = verify_otp_for_user(db, "7022359323", "1234")

    first = update_password(
        db,
        user_app_id="7022359323",
        password="newpass1",
        reset_token=verified.reset_token,
    )
    assert first.message == "UPDATED"

    # update_password closes the session; open a fresh one on same engine.
    engine = db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db2 = Session()
    try:
        second = update_password(
            db2,
            user_app_id="7022359323",
            password="newpass2",
            reset_token=verified.reset_token,
        )
        assert second.message == "RESET_TOKEN_USED"
    finally:
        db2.close()


def test_successful_password_change(db):
    _add_user(db, password="oldpass")
    upsert_otp_challenge(db, "7022359323", "1234")
    verified = verify_otp_for_user(db, "7022359323", "1234")

    result = update_password(
        db,
        user_app_id="7022359323",
        password="brand-new",
        reset_token=verified.reset_token,
    )
    assert result.message == "UPDATED"

    engine = db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db2 = Session()
    try:
        user = db2.query(User).filter(User.userAppId == "7022359323").one()
        assert user.password == "brand-new"
    finally:
        db2.close()


def test_updatepassword_without_reset_token_rejected(db):
    _add_user(db)
    result = update_password(
        db,
        user_app_id="7022359323",
        password="hacked",
        reset_token="",
    )
    assert result.message == "RESET_TOKEN_REQUIRED"

    engine = db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db2 = Session()
    try:
        user = db2.query(User).filter(User.userAppId == "7022359323").one()
        assert user.password == "oldpass"
    finally:
        db2.close()
