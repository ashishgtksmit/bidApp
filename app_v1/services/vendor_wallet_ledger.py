"""PR47 wallet ledger — one wallet per user (customer and vendor share it).

The only module allowed to change ``VendorWallet.balance_paise``.
Each ledger row records ``source`` — the app side the action came from
(``CUSTOMER`` / ``VENDOR``), or None for ops/system entries.
Callers own the DB transaction (commit / rollback); this module only flushes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..models.vendor_wallet import VendorWallet, VendorWalletTransaction

IST = ZoneInfo("Asia/Kolkata")
CURRENCY = "INR"

DIRECTION_CREDIT = "CREDIT"
DIRECTION_DEBIT = "DEBIT"
DIRECTIONS = frozenset({DIRECTION_CREDIT, DIRECTION_DEBIT})

SOURCE_CUSTOMER = "CUSTOMER"
SOURCE_VENDOR = "VENDOR"
SOURCES = frozenset({SOURCE_CUSTOMER, SOURCE_VENDOR})

TXN_TYPE_RECHARGE = "RECHARGE"
TXN_TYPE_WITHDRAWAL = "WITHDRAWAL"
TXN_TYPE_WITHDRAWAL_FEE = "WITHDRAWAL_FEE"
TXN_TYPE_WITHDRAWAL_REVERSAL = "WITHDRAWAL_REVERSAL"
TXN_TYPE_ADJUSTMENT = "ADJUSTMENT"
TXN_TYPE_SUBSCRIPTION = "SUBSCRIPTION"
TXN_TYPES = frozenset(
    {
        TXN_TYPE_RECHARGE,
        TXN_TYPE_WITHDRAWAL,
        TXN_TYPE_WITHDRAWAL_FEE,
        TXN_TYPE_WITHDRAWAL_REVERSAL,
        TXN_TYPE_ADJUSTMENT,
        TXN_TYPE_SUBSCRIPTION,
    }
)

STATUS_SUCCESS = "SUCCESS"


class WalletLedgerError(Exception):
    """Invalid ledger request (programming error, not a user error)."""


class InsufficientWalletBalance(Exception):
    """Debit exceeds available (balance - held) paise."""


def _ist_now_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _validate_amount(amount_paise: int, *, allow_zero: bool = False) -> None:
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise WalletLedgerError("amount must be int paise")
    if amount_paise < 0 or (amount_paise == 0 and not allow_zero):
        raise WalletLedgerError("amount must be positive")


def hold_funds(db: Session, *, user_uid: int, amount_paise: int) -> VendorWallet:
    """Reserve available funds (pending withdrawal). Caller commits."""
    _validate_amount(amount_paise)
    wallet = lock_or_create_wallet(db, user_uid)
    balance = int(wallet.balance_paise or 0)
    held = int(wallet.held_paise or 0)
    if amount_paise > balance - held:
        raise InsufficientWalletBalance()
    wallet.held_paise = held + amount_paise
    db.flush()
    return wallet


def release_held_funds(db: Session, *, user_uid: int, amount_paise: int) -> VendorWallet:
    """Return reserved funds to available (rejected withdrawal). Caller commits."""
    _validate_amount(amount_paise)
    wallet = lock_or_create_wallet(db, user_uid)
    held = int(wallet.held_paise or 0)
    if amount_paise > held:
        raise WalletLedgerError("release exceeds held funds")
    wallet.held_paise = held - amount_paise
    db.flush()
    return wallet


def _new_txn_ref() -> str:
    return f"WT{uuid.uuid4().hex[:24].upper()}"


def lock_or_create_wallet(db: Session, user_uid: int) -> VendorWallet:
    """Return the user's wallet row locked FOR UPDATE, creating it if absent."""
    wallet = (
        db.query(VendorWallet)
        .filter(VendorWallet.user_uid == user_uid)
        .with_for_update()
        .first()
    )
    if wallet is None:
        wallet = VendorWallet(
            user_uid=user_uid,
            balance_paise=0,
            held_paise=0,
            currency=CURRENCY,
        )
        db.add(wallet)
        db.flush()
    return wallet


def apply_wallet_entry(
    db: Session,
    *,
    user_uid: int,
    direction: str,
    txn_type: str,
    amount_paise: int,
    idempotency_key: str,
    description: Optional[str] = None,
    reference_id: Optional[str] = None,
    consume_hold_paise: int = 0,
    source: Optional[str] = None,
) -> VendorWalletTransaction:
    """Append one SUCCESS ledger row and move the balance.

    Replaying the same ``idempotency_key`` returns the original row without
    changing the balance. Raises ``InsufficientWalletBalance`` when a debit
    exceeds the available amount. ``consume_hold_paise`` settles a debit
    against funds previously reserved with ``hold_funds``.
    """
    _validate_amount(consume_hold_paise, allow_zero=True)
    if consume_hold_paise and direction != DIRECTION_DEBIT:
        raise WalletLedgerError("only debits can consume a hold")
    if direction not in DIRECTIONS:
        raise WalletLedgerError("invalid direction")
    if txn_type not in TXN_TYPES:
        raise WalletLedgerError("invalid txn_type")
    if source is not None and source not in SOURCES:
        raise WalletLedgerError("invalid source")
    _validate_amount(amount_paise)
    key = (idempotency_key or "").strip()
    if not key:
        raise WalletLedgerError("idempotency_key required")

    wallet = lock_or_create_wallet(db, user_uid)

    existing = (
        db.query(VendorWalletTransaction)
        .filter(VendorWalletTransaction.idempotency_key == key)
        .first()
    )
    if existing is not None:
        if int(existing.user_uid) != int(user_uid):
            raise WalletLedgerError("idempotency_key belongs to another wallet")
        return existing

    balance = int(wallet.balance_paise or 0)
    held = int(wallet.held_paise or 0)
    if consume_hold_paise > held:
        raise WalletLedgerError("consume_hold_paise exceeds held funds")
    if direction == DIRECTION_DEBIT:
        if amount_paise > balance - (held - consume_hold_paise):
            raise InsufficientWalletBalance()
        new_balance = balance - amount_paise
        wallet.held_paise = held - consume_hold_paise
    else:
        new_balance = balance + amount_paise

    txn = VendorWalletTransaction(
        txn_ref=_new_txn_ref(),
        user_uid=user_uid,
        direction=direction,
        txn_type=txn_type,
        status=STATUS_SUCCESS,
        amount_paise=amount_paise,
        balance_after_paise=new_balance,
        description=(description or None),
        reference_id=(reference_id or None),
        idempotency_key=key,
        source=source,
        created_at=_ist_now_naive(),
    )
    wallet.balance_paise = new_balance
    db.add(txn)
    db.flush()
    return txn
