"""PR29 — missing-location report request/response schemas."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LocationReportRequest(BaseModel):
    """Authenticated missing-location report. No email-routing or identity fields."""

    model_config = ConfigDict(extra="forbid")

    locationName: str = Field(..., min_length=1, max_length=120)
    landmark: str = Field(..., min_length=1, max_length=250)
    regionId: Optional[int] = None
    regionOther: bool = False
    usageType: Literal["PICKUP", "DROP"]

    @field_validator("locationName", "landmark", mode="before")
    @classmethod
    def _strip_required(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _region_xor(self):
        if self.regionOther:
            if self.regionId is not None:
                raise ValueError("regionId must be null when regionOther is true")
        else:
            if self.regionId is None:
                raise ValueError("regionId is required when regionOther is false")
        return self


class LocationReportResponse(BaseModel):
    message: Literal["REPORT_SUBMITTED"]
