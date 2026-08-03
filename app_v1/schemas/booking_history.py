"""PR20/PR21 minimized booking-history response schemas.

CamelCase JSON for Flutter typed history service.
Excludes PII, documents, ownership internals, and soft-error unions.
"""

from __future__ import annotations

from datetime import date, time
from typing import Optional

from pydantic import BaseModel, field_serializer


class CustomerBookingHistoryItem(BaseModel):
    """Past REQUEST - CONFIRMED row owned by JWT customer."""

    requestId: int
    requestStatus: str
    fromLocation: str
    toLocation: str
    pickupDate: date
    pickupTime: time
    noOfAdults: int
    noOfKids: int
    carType: str
    acRequested: bool
    carrierRequested: bool
    specialRequest: Optional[str] = None
    reviewDone: bool
    driverName: Optional[str] = None
    driverProfileImageUrl: Optional[str] = None
    driverGender: Optional[str] = None
    driverDateOfBirth: Optional[date] = None
    carRegistrationNumber: Optional[str] = None
    carModel: Optional[str] = None
    modelYear: Optional[int] = None

    @field_serializer("pickupDate", "driverDateOfBirth")
    def _ser_date(self, value: Optional[date]) -> Optional[str]:
        return value.isoformat() if value is not None else None

    @field_serializer("pickupTime")
    def _ser_time(self, value: time) -> str:
        return value.strftime("%H:%M:%S")

    model_config = {"from_attributes": True}


class VendorBookingHistoryItem(BaseModel):
    """Past REQUEST - CONFIRMED row won by JWT vendor."""

    requestId: int
    requestStatus: str
    fromLocation: str
    toLocation: str
    pickupDate: date
    pickupTime: time
    noOfAdults: int
    noOfKids: int
    carType: str
    acRequested: bool
    carrierRequested: bool
    specialRequest: Optional[str] = None
    finalAmount: float
    customerDisplayName: str
    customerProfileImageUrl: Optional[str] = None
    customerReviewDone: bool
    customerGeneralRating: Optional[float] = None
    customerReviewComments: Optional[str] = None
    carRegistrationNumber: Optional[str] = None
    carModel: Optional[str] = None
    driverName: Optional[str] = None

    @field_serializer("pickupDate")
    def _ser_date(self, value: date) -> str:
        return value.isoformat()

    @field_serializer("pickupTime")
    def _ser_time(self, value: time) -> str:
        return value.strftime("%H:%M:%S")

    model_config = {"from_attributes": True}


class VendorCancelledHistoryItem(BaseModel):
    """Past BOOKING - CANCELLED BY USER row won by JWT vendor (PR21)."""

    requestId: int
    requestStatus: str
    fromLocation: str
    toLocation: str
    pickupDate: date
    pickupTime: time
    noOfAdults: int
    noOfKids: int
    carType: str
    acRequested: bool
    carrierRequested: bool
    finalAmount: Optional[float] = None
    customerDisplayName: str
    customerProfileImageUrl: Optional[str] = None
    cancellationReason: str = ""

    @field_serializer("pickupDate")
    def _ser_date(self, value: date) -> str:
        return value.isoformat()

    @field_serializer("pickupTime")
    def _ser_time(self, value: time) -> str:
        return value.strftime("%H:%M:%S")

    model_config = {"from_attributes": True}
