"""
PR49 — bank-change OTP, penny-drop verification, wallet push notifications,
recharge review alerts, CSV statements.

In-memory SQLite; Cashfree, SMS, SMTP and FCM are mocked.
"""

from __future__ import annotations

import csv
import os
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi import FastAPI, HTTPException
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
from app_v1.auth.deps import AuthenticatedUser, get_current_user  # noqa: E402
from app_v1.models.otp_challenge import ApiRateLimitBucket  # noqa: E402
from app_v1.models.user_table import User  # noqa: E402
from app_v1.models.vendor_wallet import (  # noqa: E402
    VendorBankChangeOtpChallenge,
    VendorBankVerification,
    VendorWallet,
    VendorWalletOtpChallenge,
    VendorWalletRecharge,
    VendorWalletTransaction,
    VendorWalletWithdrawal,
)
from app_v1.crud import vendor_wallet_statement as statement_crud  # noqa: E402
from app_v1.crud import vendor_wallet_withdrawal as withdrawal_crud  # noqa: E402
from app_v1.crud.user import update_vendor_bank_details  # noqa: E402
from app_v1.endpoints.vendor_wallet import router as wallet_router  # noqa: E402
from app_v1.schemas.user_table import UserBankDetailsUpdate  # noqa: E402
from app_v1.services import cashfree_pg, cashfree_verification, wallet_notifications  # noqa: E402
from app_v1.services import vendor_wallet_ledger as ledger  # noqa: E402

VENDOR = "8637554388"
VENDOR_UID = 1
CUSTOMER = "8637554377"
CUSTOMER_UID = 3
OPS_KEY = "unit-test-wallet-ops-key"
FIXED_OTP = "246810"
ACCOUNT_NO = "123456789012"

TABLES = [
    User.__table__,
    ApiRateLimitBucket.__table__,
    VendorWallet.__table__,
    VendorWalletTransaction.__table__,
    VendorWalletRecharge.__table__,
    VendorWalletWithdrawal.__table__,
    VendorWalletOtpChallenge.__table__,
    VendorBankChangeOtpChallenge.__table__,
    VendorBankVerification.__table__,
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in {
        "WALLET_RECHARGE_ENABLED": "1",
        "WALLET_WITHDRAWAL_ENABLED": "1",
        "CASHFREE_ENV": "SANDBOX",
        "CASHFREE_CLIENT_ID": "unit-test-client",
        "CASHFREE_CLIENT_SECRET": "unit-test-secret",
        "CASHFREE_WALLET_NOTIFY_URL": "https://api.example.com/webhooks/cashfree/wallet",
        "CASHFREE_VERIFICATION_CLIENT_ID": "unit-test-vrs-client",
        "CASHFREE_VERIFICATION_CLIENT_SECRET": "unit-test-vrs-secret",
        "WALLET_OPS_KEY": OPS_KEY,
        "OTP_TEST_BYPASS_SMS": "1",
        "OTP_TEST_FIXED_OTP": FIXED_OTP,
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("BANK_CHANGE_OTP_REQUIRED", "BANK_VERIFICATION_ENABLED", "BANK_VERIFICATION_ACCEPTED_NAME_MATCHES"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(wallet_notifications, "notify_vendor_wallet", lambda *a: sent.append(a))
    return sent


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(
        wallet_notifications, "alert_ops_recharge_needs_review", lambda *a: sent.append(a)
    )
    return sent


@pytest.fixture(autouse=True)
def _ops_mail(monkeypatch):
    monkeypatch.setattr(withdrawal_crud, "send_email", lambda **kw: {"message": "SENT"})


def _auth(user_app_id, uid):
    return AuthenticatedUser(
        uid=uid,
        auth_subject=f"s-{user_app_id}",
        user_app_id=user_app_id,
        account_session_id="sess",
        session_version=1,
        roles=("user",),
        identity_version=2,
    )


def _add_user(db, user_app_id, uid, vendor=True, **kw):
    db.add(
        User(
            UID=uid,
            userAppId=user_app_id,
            password="secret",
            alternateNumber="1000000000",
            fullName="Vendor Name",
            emailId=kw.get("email", f"{user_app_id}@example.com"),
            dob="1990-01-01",
            city="Gangtok",
            gender="Male",
            profilePicture="images/profilepic_male.png",
            alsoVendor=vendor,
            vendorApproved=vendor,
            lockApp=False,
            customerRating="4.5",
            totalCustomerReviews=0,
            rating="4.5",
            totalNoOfReviews=0,
            joiningDate=date(2024, 1, 15),
            user_login_status="LOGGEDOUT",
            bankAccountHolderName="Vendor Name" if vendor else None,
            bankAccountNo=ACCOUNT_NO if vendor else None,
            bankIFSC="SBIN0001234" if vendor else None,
            bankName="State Bank of India" if vendor else None,
        )
    )
    db.commit()


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    _add_user(db, VENDOR, VENDOR_UID)
    _add_user(db, CUSTOMER, CUSTOMER_UID, vendor=False)
    try:
        yield db, Session
    finally:
        db.close()


def _client(Session, auth=None):
    app = FastAPI()
    app.include_router(wallet_router)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    if auth is not None:
        app.dependency_overrides[get_current_user] = lambda: auth
    return TestClient(app)


def _vendor(Session):
    return _client(Session, _auth(VENDOR, VENDOR_UID))


def _fund(db, paise):
    ledger.apply_wallet_entry(
        db,
        user_uid=VENDOR_UID,
        direction=ledger.DIRECTION_CREDIT,
        txn_type=ledger.TXN_TYPE_ADJUSTMENT,
        amount_paise=paise,
        idempotency_key=f"seed-{paise}",
    )
    db.commit()


def _bank_update(db, account="999988887777", otp=None):
    return update_vendor_bank_details(
        db,
        UserBankDetailsUpdate(
            bankAccountHolderName="Vendor Name",
            bankAccountNo=account,
            bankIFSC="HDFC0000001",
            bankName="HDFC Bank",
            otp=otp,
        ),
        user_id=VENDOR,
    )


def _account(db):
    db.expire_all()
    return db.query(User).filter(User.UID == VENDOR_UID).one().bankAccountNo


# --- Bank-change OTP -------------------------------------------------------------


def test_bank_change_without_flag_unchanged_behaviour(env):
    db, _ = env
    assert _bank_update(db).message == "UPDATED"
    assert _account(db) == "999988887777"


def test_bank_change_requires_otp_when_enabled(env, monkeypatch):
    db, Session = env
    monkeypatch.setenv("BANK_CHANGE_OTP_REQUIRED", "1")
    with pytest.raises(HTTPException) as exc:
        _bank_update(db)
    assert exc.value.detail == "BANK_CHANGE_OTP_REQUIRED"
    assert _account(db) == ACCOUNT_NO

    resp = _vendor(Session).post("/vendor/bank-details/otp")
    assert resp.status_code == 200
    assert resp.json() == {"message": "OTP_SENT", "expiresInSeconds": 300}

    with pytest.raises(HTTPException) as exc:
        _bank_update(db, otp="111111")
    assert exc.value.detail == "OTP_INVALID"
    assert _account(db) == ACCOUNT_NO

    assert _bank_update(db, otp=FIXED_OTP).message == "UPDATED"
    assert _account(db) == "999988887777"
    with pytest.raises(HTTPException) as exc:
        _bank_update(db, account="111122223333", otp=FIXED_OTP)
    assert exc.value.detail == "OTP_EXPIRED"


def test_bank_unchanged_needs_no_otp(env, monkeypatch):
    db, _ = env
    monkeypatch.setenv("BANK_CHANGE_OTP_REQUIRED", "1")
    result = update_vendor_bank_details(
        db,
        UserBankDetailsUpdate(
            bankAccountHolderName="Vendor Name",
            bankAccountNo=ACCOUNT_NO,
            bankIFSC="SBIN0001234",
            bankName="State Bank of India",
        ),
        user_id=VENDOR,
    )
    assert result.message == "UPDATED"


def test_bank_otp_allowed_for_customers(env):
    _, Session = env
    resp = _client(Session, _auth(CUSTOMER, CUSTOMER_UID)).post("/vendor/bank-details/otp")
    assert resp.status_code == 200


def test_withdrawal_and_bank_otps_are_separate(env, monkeypatch):
    db, Session = env
    monkeypatch.setenv("BANK_CHANGE_OTP_REQUIRED", "1")
    _fund(db, 300_000)
    client = _vendor(Session)
    client.post("/vendor/wallet/withdrawals/otp")
    with pytest.raises(HTTPException) as exc:
        _bank_update(db, otp=FIXED_OTP)
    assert exc.value.detail == "OTP_EXPIRED"


# --- Penny drop ------------------------------------------------------------------


def _result(valid=True, match="DIRECT_MATCH", code="ACCOUNT_IS_VALID"):
    return cashfree_verification.BankVerificationResult(
        account_valid=valid,
        account_status_code=code,
        name_match_result=match if valid else None,
        name_at_bank="VENDOR NAME" if valid else None,
        reference_id="34",
    )


def test_verification_disabled_preflight_null(env):
    db, Session = env
    body = _vendor(Session).get("/vendor/wallet/withdrawals/preflight").json()
    assert body["bankAccount"]["verificationStatus"] is None


def test_verified_account_called_once_and_noted_for_ops(env, monkeypatch):
    db, Session = env
    monkeypatch.setenv("BANK_VERIFICATION_ENABLED", "1")
    _fund(db, 300_000)
    client = _vendor(Session)
    assert client.get("/vendor/wallet/withdrawals/preflight").json()["bankAccount"]["verificationStatus"] == "NOT_VERIFIED"
    mails = []
    monkeypatch.setattr(withdrawal_crud, "send_email", lambda **kw: mails.append(kw) or {"message": "SENT"})
    with patch.object(cashfree_verification, "verify_bank_account", return_value=_result()) as verify:
        assert client.post("/vendor/wallet/withdrawals/otp").status_code == 200
        r = client.post(
            "/vendor/wallet/withdrawals",
            json={"amountPaise": 100_000, "otp": FIXED_OTP, "idempotencyKey": "wd-key-0001"},
        )
    assert r.status_code == 200
    assert verify.call_count == 1
    kwargs = verify.call_args.kwargs
    assert kwargs["bank_account"] == ACCOUNT_NO
    assert kwargs["name"] == "Vendor Name"
    assert client.get("/vendor/wallet/withdrawals/preflight").json()["bankAccount"]["verificationStatus"] == "VERIFIED"
    assert "VERIFIED" in mails[0]["message"]
    assert "VENDOR NAME" in mails[0]["message"]
    row = db.query(VendorBankVerification).one()
    assert ACCOUNT_NO not in (row.details_hash or "")


@pytest.mark.parametrize(
    "result,detail",
    [
        (_result(valid=False, code="INVALID_ACCOUNT_FAIL"), "BANK_ACCOUNT_INVALID"),
        (_result(match="NO_MATCH"), "BANK_NAME_MISMATCH"),
        (_result(match="POOR_PARTIAL_MATCH"), "BANK_NAME_MISMATCH"),
    ],
)
def test_block_invalid_and_name_mismatch_cached(env, monkeypatch, result, detail):
    db, Session = env
    monkeypatch.setenv("BANK_VERIFICATION_ENABLED", "1")
    client = _vendor(Session)
    with patch.object(cashfree_verification, "verify_bank_account", return_value=result) as verify:
        first = client.post("/vendor/wallet/withdrawals/otp")
        second = client.post("/vendor/wallet/withdrawals/otp")
    assert first.status_code == 422 and first.json()["detail"] == detail
    assert second.json()["detail"] == detail
    assert verify.call_count == 1
    assert db.query(VendorWalletOtpChallenge).count() == 0


def test_moderate_match_accepted_by_default_and_configurable(env, monkeypatch):
    db, Session = env
    monkeypatch.setenv("BANK_VERIFICATION_ENABLED", "1")
    with patch.object(cashfree_verification, "verify_bank_account", return_value=_result(match="MODERATE_PARTIAL_MATCH")):
        assert _vendor(Session).post("/vendor/wallet/withdrawals/otp").status_code == 200
    db.query(VendorBankVerification).delete()
    db.commit()
    monkeypatch.setenv("BANK_VERIFICATION_ACCEPTED_NAME_MATCHES", "DIRECT_MATCH")
    with patch.object(cashfree_verification, "verify_bank_account", return_value=_result(match="MODERATE_PARTIAL_MATCH")):
        r = _vendor(Session).post("/vendor/wallet/withdrawals/otp")
    assert r.json()["detail"] == "BANK_NAME_MISMATCH"


def test_gateway_error_not_cached(env, monkeypatch):
    db, Session = env
    monkeypatch.setenv("BANK_VERIFICATION_ENABLED", "1")
    client = _vendor(Session)
    with patch.object(cashfree_verification, "verify_bank_account", side_effect=cashfree_pg.CashfreeError("timeout")):
        r = client.post("/vendor/wallet/withdrawals/otp")
    assert r.status_code == 503 and r.json()["detail"] == "BANK_VERIFICATION_UNAVAILABLE"
    assert db.query(VendorBankVerification).count() == 0
    with patch.object(cashfree_verification, "verify_bank_account", return_value=_result()):
        assert client.post("/vendor/wallet/withdrawals/otp").status_code == 200


def test_verification_not_configured(env, monkeypatch):
    _, Session = env
    monkeypatch.setenv("BANK_VERIFICATION_ENABLED", "1")
    monkeypatch.delenv("CASHFREE_VERIFICATION_CLIENT_SECRET")
    r = _vendor(Session).post("/vendor/wallet/withdrawals/otp")
    assert r.status_code == 503


def test_changed_bank_details_need_new_verification(env, monkeypatch):
    db, Session = env
    monkeypatch.setenv("BANK_VERIFICATION_ENABLED", "1")
    client = _vendor(Session)
    with patch.object(cashfree_verification, "verify_bank_account", return_value=_result(valid=False)):
        client.post("/vendor/wallet/withdrawals/otp")
    _bank_update(db)
    assert client.get("/vendor/wallet/withdrawals/preflight").json()["bankAccount"]["verificationStatus"] == "NOT_VERIFIED"
    with patch.object(cashfree_verification, "verify_bank_account", return_value=_result()):
        assert client.post("/vendor/wallet/withdrawals/otp").status_code == 200


def _http(status, body):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


def test_verify_bank_account_request_and_parsing():
    config = cashfree_verification.VerificationConfig("SANDBOX", "cid", "csecret")
    ok = {
        "reference_id": 34,
        "name_at_bank": "JOHN DOE",
        "name_match_result": "GOOD_PARTIAL_MATCH",
        "account_status": "VALID",
        "account_status_code": "ACCOUNT_IS_VALID",
    }
    with patch.object(requests, "post", return_value=_http(200, ok)) as post:
        result = cashfree_verification.verify_bank_account(
            config, bank_account=" 123456 ", ifsc="yesb0000001", name="John* Doe!", phone="+91 98765 43210"
        )
    assert result.account_valid and result.name_match_result == "GOOD_PARTIAL_MATCH"
    assert result.reference_id == "34"
    args, kwargs = post.call_args
    assert args[0] == "https://sandbox.cashfree.com/verification/bank-account/sync"
    assert kwargs["headers"]["x-client-id"] == "cid"
    assert kwargs["json"] == {
        "bank_account": "123456",
        "ifsc": "YESB0000001",
        "name": "John Doe",
        "phone": "919876543210",
    }


@pytest.mark.parametrize(
    "status,body,valid_code",
    [
        (200, {"account_status": "INVALID", "account_status_code": "ACCOUNT_BLOCKED"}, "ACCOUNT_BLOCKED"),
        (400, {"type": "validation_error", "code": "ifsc_value_invalid"}, "IFSC_VALUE_INVALID"),
        (422, {"type": "validation_error", "code": "fraud_account"}, "FRAUD_ACCOUNT"),
    ],
)
def test_verify_definite_invalid(status, body, valid_code):
    config = cashfree_verification.VerificationConfig("SANDBOX", "cid", "csecret")
    with patch.object(requests, "post", return_value=_http(status, body)):
        result = cashfree_verification.verify_bank_account(config, bank_account="123456", ifsc="X", name="A")
    assert result.account_valid is False
    assert result.account_status_code == valid_code


@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"type": "validation_error", "code": "invalid_request", "message": "service not enabled for this account."}),
        (401, {"code": "authentication_failed"}),
        (403, {"code": "ip_validation_failed"}),
        (502, {"message": "source down"}),
        (200, {"account_status": "PENDING"}),
    ],
)
def test_verify_ambiguous_raises(status, body):
    config = cashfree_verification.VerificationConfig("SANDBOX", "cid", "csecret")
    with patch.object(requests, "post", return_value=_http(status, body)):
        with pytest.raises(cashfree_pg.CashfreeError):
            cashfree_verification.verify_bank_account(config, bank_account="123456", ifsc="X", name="A")


def test_verify_timeout_raises():
    config = cashfree_verification.VerificationConfig("SANDBOX", "cid", "csecret")
    with patch.object(requests, "post", side_effect=requests.Timeout()):
        with pytest.raises(cashfree_pg.CashfreeError):
            cashfree_verification.verify_bank_account(config, bank_account="123456", ifsc="X", name="A")


# --- Push notifications + recharge alerts ---------------------------------------


def _paid_order(order_id, amount=500.0, status="PAID"):
    return {
        "order_id": order_id,
        "cf_order_id": 1,
        "order_amount": amount,
        "order_currency": "INR",
        "order_status": status,
        "payment_session_id": "session_x",
    }


def _new_recharge(Session):
    with patch.object(cashfree_pg, "create_order", side_effect=lambda cfg, **kw: _paid_order(kw["order_id"], status="ACTIVE")):
        return _vendor(Session).post(
            "/vendor/wallet/recharges", json={"amountPaise": 50_000, "idempotencyKey": "key-00000001"}
        ).json()["orderId"]


def test_recharge_credit_pushes_once(env, pushes, alerts):
    _, Session = env
    order_id = _new_recharge(Session)
    client = _vendor(Session)
    with patch.object(cashfree_pg, "get_order", return_value=_paid_order(order_id)):
        client.get(f"/vendor/wallet/recharges/{order_id}")
        client.get(f"/vendor/wallet/recharges/{order_id}")
    assert pushes == [(VENDOR_UID, "Wallet recharged", "\u20b9500.00 has been added to your OpenBid wallet.")]
    assert alerts == []


def test_recharge_mismatch_alerts_ops_once(env, pushes, alerts):
    db, Session = env
    order_id = _new_recharge(Session)
    client = _vendor(Session)
    with patch.object(cashfree_pg, "get_order", return_value=_paid_order(order_id, amount=1.0)):
        client.get(f"/vendor/wallet/recharges/{order_id}")
        client.get(f"/vendor/wallet/recharges/{order_id}")
    assert len(alerts) == 1
    assert alerts[0][0] == order_id and alerts[0][3] == "AMOUNT_MISMATCH"
    assert pushes == []
    db.expire_all()
    assert db.query(VendorWalletRecharge).one().ops_alerted_at is not None


def test_withdrawal_lifecycle_pushes(env, pushes):
    db, Session = env
    _fund(db, 300_000)
    client = _vendor(Session)
    client.post("/vendor/wallet/withdrawals/otp")
    ref = client.post(
        "/vendor/wallet/withdrawals",
        json={"amountPaise": 100_000, "otp": FIXED_OTP, "idempotencyKey": "wd-key-0001"},
    ).json()["withdrawalId"]
    _client(Session).post(
        f"/internal/wallet/withdrawals/{ref}/paid",
        json={"utr": "UTR123456"},
        headers={"X-OpenBid-Wallet-Ops-Key": OPS_KEY},
    )
    client.post("/vendor/wallet/withdrawals/otp")
    ref2 = client.post(
        "/vendor/wallet/withdrawals",
        json={"amountPaise": 10_000, "otp": FIXED_OTP, "idempotencyKey": "wd-key-0002"},
    ).json()["withdrawalId"]
    _client(Session).post(
        f"/internal/wallet/withdrawals/{ref2}/rejected",
        json={"reason": "Name mismatch"},
        headers={"X-OpenBid-Wallet-Ops-Key": OPS_KEY},
    )
    titles = [p[1] for p in pushes]
    assert titles == ["Withdrawal requested", "Withdrawal paid", "Withdrawal requested", "Withdrawal rejected"]
    assert "UTR123456" in pushes[1][2]
    assert "Name mismatch" in pushes[3][2]


def test_notify_vendor_wallet_sends_fcm(env, monkeypatch):
    _, Session = env
    import app_v1.database as database
    from app_v1.services import notifications

    monkeypatch.setattr(database, "SessionLocal", Session)
    captured = []
    monkeypatch.setattr(
        notifications,
        "send_notification_to_user",
        lambda db, data: captured.append(data) or types.SimpleNamespace(message="NOTIFICATION_SENT"),
    )
    wallet_notifications.notify_vendor_wallet(VENDOR_UID, "T", "B")
    assert captured[0].userAppId == VENDOR
    assert captured[0].type == "wallet"
    assert captured[0].url == "//My_Wallet"
    wallet_notifications.notify_vendor_wallet(999, "T", "B")
    assert len(captured) == 1


def test_push_failure_does_not_break_withdrawal(env, monkeypatch):
    db, Session = env

    def boom(*a):
        raise RuntimeError("fcm down")

    monkeypatch.setattr(wallet_notifications, "notify_vendor_wallet", boom)
    _fund(db, 300_000)
    client = _vendor(Session)
    client.post("/vendor/wallet/withdrawals/otp")
    with pytest.raises(RuntimeError):
        # TestClient re-raises background task errors; the request itself committed.
        client.post(
            "/vendor/wallet/withdrawals",
            json={"amountPaise": 100_000, "otp": FIXED_OTP, "idempotencyKey": "wd-key-0001"},
        )
    db.expire_all()
    assert db.query(VendorWalletWithdrawal).count() == 1


# --- Statement ------------------------------------------------------------------


@pytest.fixture
def statement_mail(monkeypatch):
    sent = []

    def fake_send(**kw):
        with open(kw["attachment_path"], newline="", encoding="utf-8") as handle:
            kw["csv_rows"] = list(csv.reader(handle))
        sent.append(kw)
        return {"message": "SENT"}

    monkeypatch.setattr(statement_crud, "send_email", fake_send)
    return sent


def test_statement_emails_csv(env, statement_mail):
    db, Session = env
    _fund(db, 50_000)
    ledger.apply_wallet_entry(
        db,
        user_uid=VENDOR_UID,
        direction=ledger.DIRECTION_DEBIT,
        txn_type=ledger.TXN_TYPE_WITHDRAWAL,
        amount_paise=10_000,
        idempotency_key="d1",
        description="=HYPERLINK(evil)",
    )
    db.commit()
    today = db.query(VendorWalletTransaction).first().created_at.date().isoformat()
    r = _vendor(Session).post("/vendor/wallet/statements", json={"startDate": today, "endDate": today})
    assert r.status_code == 200
    assert r.json() == {"message": "STATEMENT_SENT", "emailMasked": "8***@example.com", "transactionCount": 2}
    mail = statement_mail[0]
    assert mail["to_address"] == f"{VENDOR}@example.com"
    assert not os.path.exists(mail["attachment_path"])
    rows = mail["csv_rows"]
    assert rows[0][0] == "Date (IST)"
    assert rows[0][4] == "Side"
    assert rows[1][4] == "OpenBid"
    assert rows[1][5] == "500.00" and rows[1][6] == ""
    assert rows[2][6] == "100.00" and rows[2][7] == "400.00"
    assert rows[2][3] == "'=HYPERLINK(evil)"


def test_statement_needs_email(env, statement_mail):
    db, Session = env
    db.query(User).filter(User.UID == VENDOR_UID).update({User.emailId: ""})
    db.commit()
    r = _vendor(Session).post("/vendor/wallet/statements", json={"startDate": "2026-01-01", "endDate": "2026-01-31"})
    assert r.status_code == 422 and r.json()["detail"] == "EMAIL_NOT_AVAILABLE"


def test_statement_range_rules(env, statement_mail):
    _, Session = env
    client = _vendor(Session)
    assert client.post("/vendor/wallet/statements", json={"startDate": "2023-01-01", "endDate": "2026-01-31"}).json()["detail"] == "WALLET_RANGE_TOO_LARGE"
    assert client.post("/vendor/wallet/statements", json={"startDate": "2026-02-01", "endDate": "2026-01-01"}).status_code == 422
    assert client.post("/vendor/wallet/statements", json={"startDate": "01-01-2026", "endDate": "2026-01-01"}).status_code == 422


def test_statement_email_failure_502(env, monkeypatch):
    _, Session = env
    monkeypatch.setattr(statement_crud, "send_email", lambda **kw: {"message": "ERROR_AUTH_FAILED"})
    r = _vendor(Session).post("/vendor/wallet/statements", json={"startDate": "2026-01-01", "endDate": "2026-01-31"})
    assert r.status_code == 502


def test_statement_rate_limited(env, statement_mail):
    _, Session = env
    client = _vendor(Session)
    codes = [
        client.post("/vendor/wallet/statements", json={"startDate": "2026-01-01", "endDate": "2026-01-31"}).status_code
        for _ in range(6)
    ]
    assert codes == [200] * 5 + [429]


def test_statement_customer_forbidden(env, statement_mail):
    _, Session = env
    r = _client(Session, _auth(CUSTOMER, CUSTOMER_UID)).post(
        "/vendor/wallet/statements", json={"startDate": "2026-01-01", "endDate": "2026-01-31"}
    )
    assert r.status_code == 403
