"""PR51 sightseeing catalog + itinerary schemas."""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

from pydantic import BaseModel, Field


class SightseeingSpotResponse(BaseModel):
    SPOTID: int
    SPOTCODE: str
    SPOTNAME: str
    LOCATIONCODE: int
    PERMITREQUIRED: bool
    TYPICALDURATIONMINS: Optional[int] = None


class ItineraryDayIn(BaseModel):
    dayNo: int = Field(..., ge=1)
    date: dt.date
    startTime: dt.time
    pickupLandmark: str = Field(..., min_length=1, max_length=200)
    # Sightseeing and local tour days need at least one spot (checked in
    # validate_itinerary); tour travel days may have none.
    spotIds: List[int] = Field(default_factory=list)
    # PR60 Tours only: the day's route and where it ends.
    fromLocation: Optional[str] = Field(None, max_length=100)
    toLocation: Optional[str] = Field(None, max_length=100)
    dropLandmark: Optional[str] = Field(None, max_length=200)
