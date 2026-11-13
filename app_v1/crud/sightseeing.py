"""PR51 sightseeing catalog + itinerary persistence/validation.

Sightseeing and (PR60) Tours are request types resolved by name from
requesttypedetails; each is disabled until its row has the exact name (launch
step). Itineraries live in request_itinerary_days / request_itinerary_day_spots
keyed by requesttable.RID. Sightseeing is 1 day in one city; a tour is 2+ days
where each day has its own route (fromLocation -> toLocation).
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
TOURS_REQUEST_TYPE_NAME = "Tours"
DEFAULT_MAX_DAYS = 15
MIN_TOUR_DAYS = 2
MAX_SPOTS_PER_DAY = 20

ERR_DISABLED = "SIGHTSEEING_DISABLED"
ERR_INVALID = "INVALID_ITINERARY"

_cached_type_id: Optional[int] = None
_cached_tours_type_id: Optional[int] = None


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
    from_location: Optional[str] = None
    to_location: Optional[str] = None
    drop_landmark: Optional[str] = None


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


def tours_max_days() -> int:
    raw = os.getenv("TOURS_MAX_DAYS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_DAYS
    except ValueError:
        value = DEFAULT_MAX_DAYS
    return max(MIN_TOUR_DAYS, value)


def _lookup_type_id(db: Session, name: str) -> Optional[int]:
    try:
        row = (
            db.query(RequestType.RTDID)
            .filter(func.lower(func.trim(RequestType.requestType)) == name.lower())
            .order_by(RequestType.RTDID)
            .first()
        )
    except SQLAlchemyError:
        logger.warning("%s request type lookup failed; treating it as disabled", name)
        return None
    return int(row[0]) if row is not None else None


def get_sightseeing_request_type_id(db: Session) -> Optional[int]:
    """RTDID of the Sightseeing request type, or None before launch."""
    global _cached_type_id
    if _cached_type_id is None:
        _cached_type_id = _lookup_type_id(db, SIGHTSEEING_REQUEST_TYPE_NAME)
    return _cached_type_id


def get_tours_request_type_id(db: Session) -> Optional[int]:
    """RTDID of the Tours request type, or None before launch (production RTDID 4 is 'Tour' until then)."""
    global _cached_tours_type_id
    if _cached_tours_type_id is None:
        _cached_tours_type_id = _lookup_type_id(db, TOURS_REQUEST_TYPE_NAME)
    return _cached_tours_type_id


def reset_sightseeing_type_cache() -> None:
    global _cached_type_id, _cached_tours_type_id
    _cached_type_id = None
    _cached_tours_type_id = None


def is_sightseeing_type(db: Session, request_type: Optional[int]) -> bool:
    if request_type is None:
        return False
    ss_id = get_sightseeing_request_type_id(db)
    return ss_id is not None and int(request_type) == ss_id


def is_tours_type(db: Session, request_type: Optional[int]) -> bool:
    if request_type is None:
        return False
    tours_id = get_tours_request_type_id(db)
    return tours_id is not None and int(request_type) == tours_id


def itinerary_type_ids(db: Session) -> List[int]:
    """Launched request types that carry an itinerary (sightseeing, tours)."""
    return [
        t for t in (get_sightseeing_request_type_id(db), get_tours_request_type_id(db)) if t is not None
    ]


def is_itinerary_type(db: Session, request_type: Optional[int]) -> bool:
    return request_type is not None and int(request_type) in itinerary_type_ids(db)


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
    """Return the validated itinerary for sightseeing / tours, None for other types.

    Raises ItineraryError when the payload does not match the request type or
    breaks an itinerary rule.
    """
    ss_id = get_sightseeing_request_type_id(db)
    tours_id = get_tours_request_type_id(db)
    if request_type is not None and tours_id is not None and int(request_type) == tours_id:
        return _validate_tour(
            db,
            itinerary=itinerary,
            from_location=from_location,
            from_landmark=from_landmark,
            to_location=to_location,
            pick_up_date=pick_up_date,
            pick_up_time=pick_up_time,
            now=now,
        )
    wants_sightseeing = ss_id is not None and request_type is not None and int(request_type) == ss_id

    if not wants_sightseeing:
        if itinerary:
            if ss_id is None and tours_id is None:
                raise ItineraryError(ERR_DISABLED, "Sightseeing is not enabled")
            raise ItineraryError(ERR_INVALID, "Itinerary is only allowed for sightseeing or tour requests")
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

    days = _ordered_days(itinerary, from_landmark, pick_up_date, pick_up_time, now)
    location_name = (from_location or "").strip().lower()
    spot_rows = _load_spots(db, {sid for d in days for sid in d.spotIds})

    validated: List[ValidatedDay] = []
    for day in days:
        landmark = _day_landmark(day)
        if not day.spotIds:
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} needs at least one spot")
        spots = []
        for sid in day.spotIds:
            row = spot_rows.get(int(sid))
            if row is None or row[1] != location_name:
                raise ItineraryError(
                    ERR_INVALID, f"Spot {sid} is not available for {from_location}"
                )
            spots.append(row[0])
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


def _ordered_days(
    itinerary: Sequence[ItineraryDayIn],
    from_landmark: Optional[str],
    pick_up_date: date,
    pick_up_time: time,
    now: datetime,
) -> List[ItineraryDayIn]:
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
    return days


def _day_landmark(day: ItineraryDayIn) -> str:
    landmark = (day.pickupLandmark or "").strip()
    if not landmark:
        raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} needs a pickup landmark")
    if len(day.spotIds) > MAX_SPOTS_PER_DAY:
        raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} has too many spots")
    if len(set(day.spotIds)) != len(day.spotIds):
        raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} repeats a spot")
    return landmark


def _load_spots(db: Session, spot_ids: set) -> Dict[int, tuple]:
    """Active spots by id -> (spot, lower-case city name)."""
    if not spot_ids:
        return {}
    rows = (
        db.query(SightseeingSpot, LocationDetail.location)
        .outerjoin(LocationDetail, LocationDetail.LID == SightseeingSpot.locationId)
        .filter(SightseeingSpot.spotId.in_(spot_ids))
        .all()
    )
    return {
        int(spot.spotId): (spot, (loc_name or "").strip().lower())
        for spot, loc_name in rows
        if spot.isActive
    }


def _validate_tour(
    db: Session,
    *,
    itinerary: Optional[Sequence[ItineraryDayIn]],
    from_location: str,
    from_landmark: Optional[str],
    to_location: Optional[str],
    pick_up_date: date,
    pick_up_time: time,
    now: datetime,
) -> ValidatedItinerary:
    if not itinerary:
        raise ItineraryError(ERR_INVALID, "Tours need an itinerary")
    max_days = tours_max_days()
    if len(itinerary) < MIN_TOUR_DAYS:
        raise ItineraryError(ERR_INVALID, f"A tour has at least {MIN_TOUR_DAYS} days")
    if len(itinerary) > max_days:
        raise ItineraryError(ERR_INVALID, f"At most {max_days} days are allowed")

    days = _ordered_days(itinerary, from_landmark, pick_up_date, pick_up_time, now)

    names = {
        (c or "").strip().lower()
        for d in days
        for c in (d.fromLocation, d.toLocation)
        if (c or "").strip()
    }
    known: Dict[str, str] = {}
    if names:
        for (loc,) in db.query(LocationDetail.location).filter(
            func.lower(func.trim(LocationDetail.location)).in_(names)
        ):
            known[str(loc).strip().lower()] = str(loc).strip()

    routes = []
    previous_to: Optional[str] = None
    for day in days:
        frm = (day.fromLocation or "").strip()
        to = (day.toLocation or "").strip()
        if not frm or not to:
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} needs a start and end city")
        for city in (frm, to):
            if city.lower() not in known:
                raise ItineraryError(ERR_INVALID, f"Day {day.dayNo}: unknown city {city}")
        if previous_to is not None and frm.lower() != previous_to.lower():
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} must start where day {day.dayNo - 1} ended")
        previous_to = to
        routes.append((known[frm.lower()], known[to.lower()]))

    if routes[0][0].lower() != (from_location or "").strip().lower():
        raise ItineraryError(ERR_INVALID, "Day 1 must start in the request's fromLocation")
    if to_location is not None and routes[-1][1].lower() != to_location.strip().lower():
        raise ItineraryError(ERR_INVALID, "The last day must end in the request's toLocation")

    spot_rows = _load_spots(db, {sid for d in days for sid in d.spotIds})
    validated: List[ValidatedDay] = []
    for day, (frm, to) in zip(days, routes):
        landmark = _day_landmark(day)
        if frm.lower() == to.lower() and not day.spotIds:
            raise ItineraryError(ERR_INVALID, f"Day {day.dayNo} stays in {frm} and needs at least one spot")
        allowed = {frm.lower(), to.lower()}
        spots = []
        for sid in day.spotIds:
            row = spot_rows.get(int(sid))
            if row is None or row[1] not in allowed:
                raise ItineraryError(ERR_INVALID, f"Spot {sid} is not available on day {day.dayNo}")
            spots.append(row[0])
        drop = (day.dropLandmark or "").strip() or None
        validated.append(
            ValidatedDay(
                day_no=day.dayNo,
                travel_date=day.date,
                start_time=day.startTime.replace(microsecond=0),
                pickup_landmark=landmark,
                spots=spots,
                from_location=frm,
                to_location=to,
                drop_landmark=drop,
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
            fromLocation=day.from_location,
            toLocation=day.to_location,
            dropLandmark=day.drop_landmark,
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
            fromLocation=day.fromLocation,
            toLocation=day.toLocation,
            dropLandmark=day.dropLandmark,
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
    """RID -> [{DAYNO, DATE, STARTTIME, PICKUPLANDMARK, FROMLOCATION, TOLOCATION,
    DROPLANDMARK, SPOTS:[{SPOTID, NAME, PERMIT}]}] (route fields are null for sightseeing)."""
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
                "FROMLOCATION": day.fromLocation,
                "TOLOCATION": day.toLocation,
                "DROPLANDMARK": day.dropLandmark,
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
