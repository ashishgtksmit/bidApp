from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from ..database import Base

_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class VendorWallet(Base):
    """One wallet per user, keyed by immutable usertable.UID (PR47).

    Shared by the customer and vendor sides of the app (single wallet,
    2026-11-12); the table keeps its PR47 name.

    Amounts are integer paise. ``held_paise`` is reserved for pending
    withdrawals; available = balance - held. Rows are only changed through
    ``services.vendor_wallet_ledger`` under a row lock.
    """

    __tablename__ = "vendor_wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uid = Column(BigInteger, nullable=False, unique=True, index=True)
    balance_paise = Column(BigInteger, nullable=False, default=0)
    held_paise = Column(BigInteger, nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="INR")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorWalletTransaction(Base):
    """Append-only wallet ledger row (PR47).

    ``txn_ref`` is the only identifier exposed to clients.
    ``idempotency_key`` prevents the same business event (gateway order,
    withdrawal request) from being applied twice.
    """

    __tablename__ = "vendor_wallet_transactions"

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    txn_ref = Column(String(40), nullable=False, unique=True)
    user_uid = Column(BigInteger, nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    txn_type = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False)
    amount_paise = Column(BigInteger, nullable=False)
    balance_after_paise = Column(BigInteger, nullable=False)
    description = Column(String(255), nullable=True)
    reference_id = Column(String(64), nullable=True)
    idempotency_key = Column(String(100), nullable=True, unique=True)
    # App side that caused the entry: CUSTOMER / VENDOR; NULL = ops/system.
    source = Column(String(10), nullable=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorWalletRecharge(Base):
    """Cashfree recharge order (PR48).

    Status: CREATE_PENDING -> PENDING -> PAID, or CREATE_FAILED / FAILED /
    EXPIRED. PAID is terminal. Credit happens only after a server-side order
    fetch whose amount and currency match this row.
    """

    __tablename__ = "vendor_wallet_recharges"
    __table_args__ = (
        UniqueConstraint("user_uid", "client_idempotency_key", name="uq_wallet_recharge_client_key"),
    )

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    order_id = Column(String(40), nullable=False, unique=True)
    user_uid = Column(BigInteger, nullable=False, index=True)
    amount_paise = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    status = Column(String(20), nullable=False)
    environment = Column(String(12), nullable=False)
    client_idempotency_key = Column(String(64), nullable=False)
    cf_order_id = Column(String(64), nullable=True)
    payment_session_id = Column(String(255), nullable=True)
    failure_code = Column(String(40), nullable=True)
    credited_txn_ref = Column(String(40), nullable=True)
    ops_alerted_at = Column(DateTime, nullable=True)
    # App side the recharge was started from: CUSTOMER / VENDOR.
    source = Column(String(10), nullable=True)
    created_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorWalletWithdrawal(Base):
    """Manual-payout withdrawal request (PR48).

    REQUESTED holds amount + fee in ``VendorWallet.held_paise``. Ops closes it:
    PAID debits the hold (with UTR), REJECTED releases it. Bank details are a
    snapshot at request time; the full account number is only ever sent to
    the ops mailbox and internal ops endpoints, never to the vendor API.
    """

    __tablename__ = "vendor_wallet_withdrawals"
    __table_args__ = (
        UniqueConstraint("user_uid", "client_idempotency_key", name="uq_wallet_withdrawal_client_key"),
    )

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    withdrawal_ref = Column(String(40), nullable=False, unique=True)
    user_uid = Column(BigInteger, nullable=False, index=True)
    amount_paise = Column(BigInteger, nullable=False)
    fee_paise = Column(BigInteger, nullable=False)
    status = Column(String(20), nullable=False)
    client_idempotency_key = Column(String(64), nullable=False)
    bank_account_holder_name = Column(String(300), nullable=False)
    bank_account_no = Column(String(100), nullable=False)
    bank_ifsc = Column(String(20), nullable=False)
    bank_name = Column(String(255), nullable=True)
    ops_email_status = Column(String(20), nullable=True)
    bank_verification_note = Column(String(255), nullable=True)
    # App side the withdrawal was requested from: CUSTOMER / VENDOR.
    source = Column(String(10), nullable=True)
    # Signed one-time ops email links (paid / rejected HTML forms).
    ops_link_paid_jti_hash = Column(String(64), nullable=True)
    ops_link_reject_jti_hash = Column(String(64), nullable=True)
    ops_link_expires_at = Column(DateTime, nullable=True)
    utr = Column(String(40), nullable=True)
    rejection_reason = Column(String(255), nullable=True)
    requested_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class _OtpChallengeColumns:
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uid = Column(BigInteger, nullable=False, unique=True, index=True)
    otp_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorWalletOtpChallenge(_OtpChallengeColumns, Base):
    """Hashed SMS OTP for confirming a withdrawal (PR48). One per user."""

    __tablename__ = "vendor_wallet_otp_challenges"


class VendorBankChangeOtpChallenge(_OtpChallengeColumns, Base):
    """Hashed SMS OTP for changing bank details (PR49, behind
    ``BANK_CHANGE_OTP_REQUIRED``). One per user."""

    __tablename__ = "vendor_bank_change_otp_challenges"


class VendorBankVerification(Base):
    """Cached Cashfree penny-drop result (PR49).

    Keyed by a peppered hash of uid + account + IFSC + holder name, so a
    result only applies to the exact details verified and no account number
    is stored here. Only definite results are cached (one paid API call per
    set of details).
    """

    __tablename__ = "vendor_bank_verifications"
    __table_args__ = (
        UniqueConstraint("user_uid", "details_hash", name="uq_bank_verification_details"),
    )

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    user_uid = Column(BigInteger, nullable=False, index=True)
    details_hash = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False)
    account_status_code = Column(String(40), nullable=True)
    name_match_result = Column(String(40), nullable=True)
    name_at_bank = Column(String(255), nullable=True)
    reference_id = Column(String(40), nullable=True)
    environment = Column(String(12), nullable=False)
    created_at = Column(DateTime, nullable=False)
