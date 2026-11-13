"""PR51 sightseeing catalog + itinerary persistence/validation.

Sightseeing is a request type resolved by name from requesttypedetails; it is
disabled until that row exists (launch step). Itineraries live in
request_itinerary_days / request_itinerary_day_spots keyed by requesttable.RID.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.location_details import LocationDetail
from ..models.request_type_details import RequestType
from ..models.sightseeing import (
    RequestItineraryDay,
    RequestItineraryDaySpot,
    SightseeingSpot,
)
from ..schemas.sightseeing import ItineraryDayIn, SightseeingSpotResponse

logger = logging.getLogger(__name__)

SIGHTSEEING_REQUEST_TYPE_NAME = "Sightseeing"
DEFAULT_MAX_DAYS = 15
MAX_SPOTS_PER_DAY = 20

ERR_DISABLED = "SIGHTSEEING_DISABLED"
ERR_INVALID = "INVALID_ITINERARY"

_cached_type_id: Optional[int] = None


class ItineraryError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class ValidatedDay:
    day_no: int
    travel_date: date
    start_time: time
    pickup_landmark: str
    spots: List[SightseeingSpot] = field(default_factory=list)


@dataclass
class ValidatedItinerary:
    days: List[ValidatedDay]

    @property
    def trip_end_date(self) -> date:
        return self.days[-1].travel_date


def sightseeing_max_days() -> int:
    raw = os.getenv("SIGHTSEEING_MAX_DAYS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_DAYS
    except ValueError:
        value = DEFAULT_MAX_DAYS
    return max(1, value)


def get_sightseeing_request_type_id(db: Session) -> Optional[int]:
    """RTDID of the Sightseeing request type, or None before launch."""
    global _cached_type_id
    if _cached_type_id is not None:
        return _cached_type_id
    try:
        row = (
            db.query(RequestType.RTDID)
            .filter(func.lower(func.trim(RequestType.requestType)) == SIGHTSEEING_REQUEST_TYPE_NAME.lower())
            .order_by(RequestType.RTDID)
            .first()
        )
    except SQLAlchemyError:
        logger.warning("sightseeing request type lookup failed; treating sightseeing as disabled")
        return None
    if row is not None:
        _cached_type_id = int(row[0])
    return _cached_type_id


def reset_sightseeing_type_cache() -> None:
    global _cached_type_id
    _cached_type_id = None


def is_sightseeing_type(db: Session, request_type: Optional[int]) -> bool:
    if request_type is None:
        return False
    ss_id = get_sightseeing_request_type_id(db)
    return ss_id is not None and int(request_type) == ss_id


def list_spots_for_location(db: Session, location_id: int) -> List[SightseeingSpotResponse]:
    spots = (
        db.query(SightseeingSpot)
        .filter(SightseeingSpot.locationId == location_id, SightseeingSpot.isActive.is_(True))
        .order_by(SightseeingSpot.sortOrder, SightseeingSpot.spotName)
        .all()
    )
    return [
        SightseeingSpotResponse(
            SPOTID=int(s.spotId),
            SPOTCODE=s.spotCode,
            SPOTNAME=s.spotName,
            LOCATIONCODE=int(s.locationId),
            PERMITREQUIRED=bool(s.permitRequired),
            TYPICALDURATIONMINS=s.typicalDurationMins,
        )
        for s in spots
    ]


def validate_itinerary(
    db: Session,
    *,
    request_type: Optional[int],
    itinerary: Optional[Sequence[ItineraryDayIn]],
    from_location: str,
    from_landmark: Optional[str],
    pick_up_date: date,
    pick_up_time: time,
    now: datetime,
    to_location: Optional[str] = None,
) -> Optional[ValidatedItinerary]:
    """Return the validated itinerary for sightseeing, None for other types.

    Raises ItineraryError when the payload does not match the request type or
    breaks an itinerary rule.
    """
    ss_id = get_sightseeing_request_type_id(db)
    wants_sightseeing = ss_id is not None and request_type is not None and int(request_type) == ss_id

    if not wants_sightseeing:
        if itinerary:
            if ss_id is None:
                raise ItineraryError(ERR_DISABLED, "Sightseeing is not enabled")
            raise ItineraryError(ERR_INVALID, "Itinerary is only allowed for sightseeing requests")
        return None

    if not itinerary:
        raise ItineraryError(ERR_INVALID, "Sightseeing requests need an itinerary")
    max_days = sightseeing_max_days()
    if len(itinerary) > max_days:
        raise ItineraryError(ERR_INVALID, f"At most {max_days} days are allowed")
    # Sightseeing is a single day that starts and ends in the base city;
    # multi-day or city-to-city trips belong to Tours.
    if len(itinerary) != 1:
        raise ItineraryError(ERR_INVALID, "Sightseeing is a single day")
    if to_location is not None and to_location.strip().lower() != (from_location or "").strip().lower():
        raise ItineraryError(ERR_INVALID, "Sightseeing must start and end in the same city")

    days = sorted(itinerary, key=lambda d: d.dayNo)
    if [d.dayNo for d in days] != list(range(1, len(days) + 1)):
        raise ItineraryError(ERR_INVALID, "Day numbers must be 1..N without gaps")
    for prev, cur in zip(days, days[1:]):
        if cur.date <= prev.date:
            raise ItineraryError(ERR_INVALID, "Day dates must be unique and ascending")

    first = days[0]
    if datetime.combine(first.date, first.startTime) <= now:
        raise ItineraryError(ERR_INVALID, "Day 1 must be in the future")
    if first.date != pick_up_date or first.startTime.replace(microsecond=0) != pick_up_time.replace(microsecond=0):
        raise ItineraryError(ERR_INVALID, "Day 1 must match pickUpDate and pickUpTime")
    if (first.pickupLandmark or "").strip() != (from_landmark or "").strip():
        raise ItineraryError(ERR_INVALID, "Day 1 pickup must match fromLandmark")

    all_ids = {sid for d in days for sid in d.spotIds}
    location_name = (from_location or "").strip().lower()
    rows = (
        db.query(SightseeingSpot, LocationDetail.location)
        .outerjoin(LocationDetail, LocationDetail.LID == SightseeingSpot.locationId)
        .filter(SightseeingSpot.spotId.in_(all_ids))
        .all()
    )
    spot_by_id: Dict[int, SightseeingSpot] = {}
    for spot, loc_name in rows:
        if not spot.isActive:
            continue
        if (loc_name or "").strip().lower() != location_name:
            continue
        spot_by_id[int(spot.spotId)] = spot

    validated: List[ValidatedDay] = []
    for day in days:
        landmark = (day.pickupLandmark or "").strip()
        if not landmark:
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} needs a pickup landmark")
        if len(day.spotIds) > MAX_SPOTS_PER_DAY:
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} has too many spots")
        if len(set(day.spotIds)) != len(day.spotIds):
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} repeats a spot")
        spots = []
        for sid in day.spotIds:
            spot = spot_by_id.get(int(sid))
            if spot is None:
                raise ItineraryError(
                    ERR_INVALID, f"Spot {sid} is not available for {from_location}"
                )
            spots.append(spot)
        validated.append(
            ValidatedDay(
                day_no=day.dayNo,
                travel_date=day.date,
                start_time=day.startTime.replace(microsecond=0),
                pickup_landmark=landmark,
                spots=spots,
            )
        )
    return ValidatedItinerary(days=validated)


def write_itinerary(db: Session, request_id: int, itinerary: ValidatedItinerary) -> None:
    """Insert day + spot rows for a request (caller owns the transaction)."""
    for day in itinerary.days:
        row = RequestItineraryDay(
            requestId=request_id,
            dayNo=day.day_no,
            travelDate=day.travel_date,
            startTime=day.start_time,
            pickupLandmark=day.pickup_landmark,
        )
        db.add(row)
        db.flush()
        for seq, spot in enumerate(day.spots, start=1):
            db.add(
                RequestItineraryDaySpot(
                    dayId=row.dayId,
                    spotId=spot.spotId,
                    sequenceNo=seq,
                    spotNameSnapshot=spot.spotName,
                    permitRequiredSnapshot=bool(spot.permitRequired),
                )
            )
    db.flush()


def delete_itinerary(db: Session, request_id: int) -> None:
    day_ids = [
        d[0]
        for d in db.query(RequestItineraryDay.dayId)
        .filter(RequestItineraryDay.requestId == request_id)
        .all()
    ]
    if day_ids:
        db.query(RequestItineraryDaySpot).filter(
            RequestItineraryDaySpot.dayId.in_(day_ids)
        ).delete(synchronize_session=False)
        db.query(RequestItineraryDay).filter(
            RequestItineraryDay.dayId.in_(day_ids)
        ).delete(synchronize_session=False)
    db.flush()


def clone_itinerary(db: Session, source_request_id: int, target_request_id: int) -> int:
    """Copy days + spot snapshots from one request to another. Returns day count."""
    days = (
        db.query(RequestItineraryDay)
        .filter(RequestItineraryDay.requestId == source_request_id)
        .order_by(RequestItineraryDay.dayNo)
        .all()
    )
    for day in days:
        new_day = RequestItineraryDay(
            requestId=target_request_id,
            dayNo=day.dayNo,
            travelDate=day.travelDate,
            startTime=day.startTime,
            pickupLandmark=day.pickupLandmark,
        )
        db.add(new_day)
        db.flush()
        spots = (
            db.query(RequestItineraryDaySpot)
            .filter(RequestItineraryDaySpot.dayId == day.dayId)
            .order_by(RequestItineraryDaySpot.sequenceNo)
            .all()
        )
        for spot in spots:
            db.add(
                RequestItineraryDaySpot(
                    dayId=new_day.dayId,
                    spotId=spot.spotId,
                    sequenceNo=spot.sequenceNo,
                    spotNameSnapshot=spot.spotNameSnapshot,
                    permitRequiredSnapshot=bool(spot.permitRequiredSnapshot),
                )
            )
    db.flush()
    return len(days)


def load_itineraries(db: Session, request_ids: Iterable[int]) -> Dict[int, List[dict]]:
    """RID -> [{DAYNO, DATE, STARTTIME, PICKUPLANDMARK, SPOTS:[{SPOTID, NAME, PERMIT}]}]."""
    rids = sorted({int(r) for r in request_ids if r is not None})
    if not rids:
        return {}
    rows = (
        db.query(RequestItineraryDay, RequestItineraryDaySpot)
        .outerjoin(RequestItineraryDaySpot, RequestItineraryDaySpot.dayId == RequestItineraryDay.dayId)
        .filter(RequestItineraryDay.requestId.in_(rids))
        .order_by(
            RequestItineraryDay.requestId,
            RequestItineraryDay.dayNo,
            RequestItineraryDaySpot.sequenceNo,
        )
        .all()
    )
    result: Dict[int, List[dict]] = {}
    day_index: Dict[int, dict] = {}
    for day, spot in rows:
        entry = day_index.get(int(day.dayId))
        if entry is None:
            entry = {
                "DAYNO": int(day.dayNo),
                "DATE": day.travelDate.isoformat(),
                "STARTTIME": day.startTime.strftime("%H:%M:%S"),
                "PICKUPLANDMARK": day.pickupLandmark,
                "SPOTS": [],
            }
            day_index[int(day.dayId)] = entry
            result.setdefault(int(day.requestId), []).append(entry)
        if spot is not None:
            entry["SPOTS"].append(
                {
                    "SPOTID": int(spot.spotId),
                    "NAME": spot.spotNameSnapshot,
                    "PERMIT": bool(spot.permitRequiredSnapshot),
                }
            )
    return result
