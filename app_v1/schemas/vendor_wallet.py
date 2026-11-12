"""PR47 vendor wallet response schemas.

CamelCase JSON for the Flutter wallet service. Amounts are integer paise.
Never exposes internal row ids, UID, phone, or idempotency keys.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class VendorWalletSummary(BaseModel):
    balancePaise: int
    heldPaise: int
    availablePaise: int
    currency: str = Field(default="INR")


class VendorWalletTransactionItem(BaseModel):
    transactionId: str
    direction: str
    type: str
    status: str
    amountPaise: int
    balanceAfterPaise: int
    description: Optional[str] = None
    # App side that caused the entry: CUSTOMER / VENDOR; null = ops/system.
    source: Optional[str] = None
    createdAt: str


class VendorWalletTransactionPage(BaseModel):
    items: List[VendorWalletTransactionItem]
    nextCursor: Optional[str] = None


# --- PR48 recharge ----------------------------------------------------------


class WalletRechargeCreateRequest(BaseModel):
    amountPaise: int
    idempotencyKey: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9\-_]+$")


class WalletRechargeOrder(BaseModel):
    orderId: str
    status: str
    amountPaise: int
    environment: str
    paymentSessionId: Optional[str] = None
    creditedTransactionId: Optional[str] = None
    source: Optional[str] = None


# --- PR48 withdrawal --------------------------------------------------------


class WalletBankAccountSummary(BaseModel):
    hasBankAccount: bool
    maskedAccountNumber: Optional[str] = None
    accountHolderName: Optional[str] = None
    bankIFSC: Optional[str] = None
    bankName: Optional[str] = None
    # PR49 penny drop: null when disabled; VERIFIED / INVALID / NAME_MISMATCH / NOT_VERIFIED.
    verificationStatus: Optional[str] = None


class WalletFeeSlab(BaseModel):
    upToPaise: Optional[int] = None
    feePaise: int


class WalletWithdrawalPreflight(BaseModel):
    bankAccount: WalletBankAccountSummary
    availablePaise: int
    minPaise: int
    maxPaise: int
    dailyLimitPaise: int
    dailyRemainingPaise: int
    feeSlabs: List[WalletFeeSlab]
    # e.g. VENDOR_ACCESS_RESTRICTED when withdrawal is not allowed; null = allowed.
    withdrawalBlockedReason: Optional[str] = None


class WalletWithdrawalOtpResponse(BaseModel):
    message: str
    expiresInSeconds: int


class WalletWithdrawalCreateRequest(BaseModel):
    amountPaise: int
    otp: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")
    idempotencyKey: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9\-_]+$")


class WalletWithdrawalItem(BaseModel):
    withdrawalId: str
    status: str
    amountPaise: int
    feePaise: int
    totalPaise: int
    maskedAccountNumber: str
    bankName: Optional[str] = None
    requestedAt: str
    resolvedAt: Optional[str] = None
    utr: Optional[str] = None
    rejectionReason: Optional[str] = None
    source: Optional[str] = None


class WalletWithdrawalList(BaseModel):
    items: List[WalletWithdrawalItem]


# --- PR48 ops (internal key only) -------------------------------------------


class OpsWithdrawalItem(WalletWithdrawalItem):
    vendorName: Optional[str] = None
    vendorPhone: Optional[str] = None
    accountHolderName: str
    accountNumber: str
    bankIFSC: str
    opsEmailStatus: Optional[str] = None
    bankVerification: Optional[str] = None


class OpsWithdrawalList(BaseModel):
    items: List[OpsWithdrawalItem]


class OpsWithdrawalPaidRequest(BaseModel):
    utr: str = Field(min_length=6, max_length=40, pattern=r"^[A-Za-z0-9]+$")


class OpsWithdrawalRejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=255)


# --- PR49 bank OTP / statements ------------------------------------------------


class OtpSentResponse(BaseModel):
    message: str
    expiresInSeconds: int


class WalletStatementRequest(BaseModel):
    startDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class WalletStatementResponse(BaseModel):
    message: str
    emailMasked: str
    transactionCount: int
