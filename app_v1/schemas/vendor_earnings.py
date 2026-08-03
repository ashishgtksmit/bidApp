"""PR22 vendor earnings / reporting response schemas.

CamelCase JSON for Flutter typed earnings service.
Represents gross completed booking value — not paid/net/settlement.
Excludes PII, payment internals, ownership fields, and soft-error unions.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer


class VendorEarningsSummary(BaseModel):
    completedTripCount: int
    grossBookingValue: int
    currency: str = Field(default="INR")


class VendorEarningsBucket(BaseModel):
    periodStart: date
    periodEnd: date
    label: str
    completedTripCount: int
    grossBookingValue: int

    @field_serializer("periodStart", "periodEnd")
    def _ser_date(self, value: date) -> str:
        return value.isoformat()


class VendorEarningTripItem(BaseModel):
    requestId: int
    pickupDate: str
    pickupTime: str
    fromLocation: str
    toLocation: str
    grossAmount: int
    requestStatus: str


class VendorEarningsReport(BaseModel):
    periodStart: Optional[str] = None
    periodEnd: Optional[str] = None
    summary: VendorEarningsSummary
    monthlyBuckets: List[VendorEarningsBucket]
    trips: List[VendorEarningTripItem]
