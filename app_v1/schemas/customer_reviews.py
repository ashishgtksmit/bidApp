"""PR19 public-safe customer review schemas.

Create body accepts only RID + RATING + COMMENTS.
Identity is derived from JWT + request row.
Half-step / text validation is enforced in CRUD with hard HTTPException details.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Union

from pydantic import BaseModel, field_validator


class CreateCustomerReview(BaseModel):
    """Vendor → customer review insert (PR19)."""

    RID: int
    RATING: Any
    COMMENTS: Optional[Any] = ""

    @field_validator("RID")
    @classmethod
    def rid_positive(cls, v: int) -> int:
        if isinstance(v, bool) or v is None or int(v) <= 0:
            raise ValueError("INVALID_RID")
        return int(v)

    model_config = {"from_attributes": True}


class CustomerReviewSummaryResponse(BaseModel):
    """Public-safe customer (passenger) review list item (PR19)."""

    reviewId: int
    requestId: int
    generalRating: float
    comments: str = ""
    travelDate: Optional[date] = None
    fromLocation: Optional[str] = None
    toLocation: Optional[str] = None
    reviewerDisplayName: Optional[str] = None
    reviewerProfileImageUrl: Optional[str] = None

    model_config = {"from_attributes": True}


class CustomerReviewInsertResponse(BaseModel):
    message: str = "INSERTED"


class CustomerReviewDetail(CustomerReviewSummaryResponse):
    pass


class NoReviewResponse(BaseModel):
    message: str


class UpdateCustomerReview(BaseModel):
    generalRating: Optional[Union[str, float]] = None
    comments: Optional[str] = None

    model_config = {"from_attributes": True}
