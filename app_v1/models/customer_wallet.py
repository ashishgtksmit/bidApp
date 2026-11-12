from sqlalchemy import BigInteger, Column, DateTime, Integer, String, func

from ..database import Base

_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class CustomerWallet(Base):
    """Customer wallet, separate from the vendor wallet (PR50).

    A user who is also a vendor has one row here and one in
    ``vendor_wallets``; the balances never mix. Same columns and rules as
    ``VendorWallet``; rows change only through ``services.vendor_wallet_ledger``
    with ``wallet_model=CustomerWallet``.
    """

    __tablename__ = "customer_wallets"

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


class CustomerWalletTransaction(Base):
    """Append-only customer ledger row (PR50). Same columns as
    ``VendorWalletTransaction``; ``txn_ref`` starts with ``CT``."""

    __tablename__ = "customer_wallet_transactions"

    TXN_REF_PREFIX = "CT"

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
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
