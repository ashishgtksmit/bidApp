"""PR19 public-safe vendor review schemas.

Create body accepts only RID + category ratings + comments.
Identity (reviewer + target vendor) is derived from JWT + request row.
Half-step / text validation is enforced in CRUD with hard HTTPException details.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Union

from pydantic import BaseModel, field_validator


class ReviewCreate(BaseModel):
    """Customer → vendor review insert (PR19). Client sends RID only for identity."""

    RID: int
    driverBehaviour: Any
    punctuality: Any
    carCondition: Any
    cleanliness: Any
    comments: Optional[Any] = ""

    @field_validator("RID")
    @classmethod
    def rid_positive(cls, v: int) -> int:
        if isinstance(v, bool) or v is None or int(v) <= 0:
            raise ValueError("INVALID_RID")
        return int(v)

    model_config = {"from_attributes": True}


class VendorReviewSummaryResponse(BaseModel):
    """Public-safe vendor review list item (PR19)."""

    reviewId: int
    requestId: int
    travelDate: Optional[date] = None
    driverBehaviour: float
    punctuality: float
    carCondition: float
    cleanliness: float
    comments: str = ""
    reviewerDisplayName: Optional[str] = None
    reviewerProfileImageUrl: Optional[str] = None
    fromLocation: Optional[str] = None
    toLocation: Optional[str] = None
    carRegNo: Optional[str] = None
    carModel: Optional[str] = None
    driverName: Optional[str] = None

    model_config = {"from_attributes": True}


class ReviewInsertResponse(BaseModel):
    message: str = "INSERTED"


class Review(BaseModel):
    driverBehaviour: Union[int, float]
    punctuality: Union[int, float]
    carCondition: Union[int, float]
    cleanliness: Union[int, float]
    refreshments: Optional[int] = 0
    comments: Optional[str] = None

    model_config = {"from_attributes": True}


class ReviewDetail(VendorReviewSummaryResponse):
    pass


class NoReviewResponse(BaseModel):
    message: str
