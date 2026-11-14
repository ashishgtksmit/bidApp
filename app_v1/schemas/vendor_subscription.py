"""PR63 vendor subscription schemas. CamelCase JSON, amounts in integer paise."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .vendor_wallet import WalletRechargeOrder

_KEY = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9\-_]+$")
_PLAN_CODE = Field(default="MONTHLY", min_length=1, max_length=16, pattern=r"^[A-Z0-9_]+$")


class VendorSubscriptionPlanInfo(BaseModel):
    code: str
    name: str
    pricePaise: int
    durationDays: int


class VendorSubscriptionStatus(BaseModel):
    # Server feature flags: the app hides subscription UI when not enabled.
    enabled: bool
    enforced: bool
    active: bool
    # ACTIVE / EXPIRED / NONE (never subscribed)
    status: str
    currentPeriodEnd: Optional[str] = None
    daysLeft: int = 0
    autoRenew: bool = True
    # Null when no active plan is configured in the database.
    plan: Optional[VendorSubscriptionPlanInfo] = None
    walletAvailablePaise: int = 0


class VendorSubscriptionPurchaseRequest(BaseModel):
    planCode: str = _PLAN_CODE
    idempotencyKey: str = _KEY


class VendorSubscriptionAutoRenewRequest(BaseModel):
    enabled: bool


class VendorSubscriptionCheckoutStatus(BaseModel):
    order: WalletRechargeOrder
    subscription: VendorSubscriptionStatus


class VendorSubscriptionPeriodItem(BaseModel):
    planCode: str
    amountPaise: int
    periodStart: str
    periodEnd: str
    # WALLET / CASHFREE / AUTO_RENEW / OPS_GRANT
    paymentMethod: str
    transactionId: Optional[str] = None
    createdAt: str


class VendorSubscriptionHistory(BaseModel):
    items: List[VendorSubscriptionPeriodItem]


# --- Internal (ops / worker) -------------------------------------------------


class OpsSubscriptionPlanItem(VendorSubscriptionPlanInfo):
    active: bool


class OpsSubscriptionPlanList(BaseModel):
    items: List[OpsSubscriptionPlanItem]


class OpsSubscriptionPlanUpdate(BaseModel):
    """Create or update a plan. Omitted fields keep their current value;
    creating a new plan needs name, pricePaise and durationDays."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    pricePaise: Optional[int] = Field(default=None, ge=100, le=10_000_000)
    durationDays: Optional[int] = Field(default=None, ge=1, le=730)
    active: Optional[bool] = None


class OpsSubscriptionGrantRequest(BaseModel):
    userAppId: str = Field(min_length=10, max_length=15, pattern=r"^\d+$")
    days: int = Field(ge=1, le=366)
    reason: str = Field(min_length=3, max_length=200)
    # Makes a retried grant apply once.
    idempotencyKey: str = _KEY


class OpsSubscriptionGrantResponse(BaseModel):
    userAppId: str
    currentPeriodEnd: str
    applied: bool


class SubscriptionDailyRunResult(BaseModel):
    renewed: int
    renewFailed: int
    reminded: int
    expiredNotified: int
