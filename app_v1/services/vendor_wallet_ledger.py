"""PR47 vendor wallet ledger (PR50: also customer wallets).

The only module allowed to change wallet balances. Defaults to the vendor
tables; pass ``wallet_model=CustomerWallet`` and
``txn_model=CustomerWalletTransaction`` for the customer wallet.
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

TXN_TYPE_RECHARGE = "RECHARGE"
TXN_TYPE_WITHDRAWAL = "WITHDRAWAL"
TXN_TYPE_WITHDRAWAL_FEE = "WITHDRAWAL_FEE"
TXN_TYPE_WITHDRAWAL_REVERSAL = "WITHDRAWAL_REVERSAL"
TXN_TYPE_ADJUSTMENT = "ADJUSTMENT"
TXN_TYPES = frozenset(
    {
        TXN_TYPE_RECHARGE,
        TXN_TYPE_WITHDRAWAL,
        TXN_TYPE_WITHDRAWAL_FEE,
        TXN_TYPE_WITHDRAWAL_REVERSAL,
        TXN_TYPE_ADJUSTMENT,
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


def hold_funds(
    db: Session, *, user_uid: int, amount_paise: int, wallet_model=VendorWallet
):
    """Reserve available funds (pending withdrawal). Caller commits."""
    _validate_amount(amount_paise)
    wallet = lock_or_create_wallet(db, user_uid, wallet_model=wallet_model)
    balance = int(wallet.balance_paise or 0)
    held = int(wallet.held_paise or 0)
    if amount_paise > balance - held:
        raise InsufficientWalletBalance()
    wallet.held_paise = held + amount_paise
    db.flush()
    return wallet


def release_held_funds(
    db: Session, *, user_uid: int, amount_paise: int, wallet_model=VendorWallet
):
    """Return reserved funds to available (rejected withdrawal). Caller commits."""
    _validate_amount(amount_paise)
    wallet = lock_or_create_wallet(db, user_uid, wallet_model=wallet_model)
    held = int(wallet.held_paise or 0)
    if amount_paise > held:
        raise WalletLedgerError("release exceeds held funds")
    wallet.held_paise = held - amount_paise
    db.flush()
    return wallet


def _new_txn_ref(txn_model) -> str:
    prefix = getattr(txn_model, "TXN_REF_PREFIX", "WT")
    return f"{prefix}{uuid.uuid4().hex[:24].upper()}"


def lock_or_create_wallet(db: Session, user_uid: int, *, wallet_model=VendorWallet):
    """Return the user's wallet row locked FOR UPDATE, creating it if absent."""
    wallet = (
        db.query(wallet_model)
        .filter(wallet_model.user_uid == user_uid)
        .with_for_update()
        .first()
    )
    if wallet is None:
        wallet = wallet_model(
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
    wallet_model=VendorWallet,
    txn_model=VendorWalletTransaction,
):
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
    _validate_amount(amount_paise)
    key = (idempotency_key or "").strip()
    if not key:
        raise WalletLedgerError("idempotency_key required")

    wallet = lock_or_create_wallet(db, user_uid, wallet_model=wallet_model)

    existing = (
        db.query(txn_model)
        .filter(txn_model.idempotency_key == key)
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

    txn = txn_model(
        txn_ref=_new_txn_ref(txn_model),
        user_uid=user_uid,
        direction=direction,
        txn_type=txn_type,
        status=STATUS_SUCCESS,
        amount_paise=amount_paise,
        balance_after_paise=new_balance,
        description=(description or None),
        reference_id=(reference_id or None),
        idempotency_key=key,
        created_at=_ist_now_naive(),
    )
    wallet.balance_paise = new_balance
    db.add(txn)
    db.flush()
    return txn
