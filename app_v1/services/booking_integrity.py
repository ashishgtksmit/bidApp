"""PR64 booking integrity — vehicle overlap and related-party (self-deal) checks."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable, Optional, Set

from fastapi import HTTPException, status
from sqlalchemy import and_
from sqlalchemy.orm import Session

from ..models.bid_details import BidDetail
from ..models.request_table import Request
from ..models.user_devices import UserDevice
from ..models.user_table import User

DETAIL_RELATED_PARTY = "RELATED_PARTY"
DETAIL_VEHICLE_ALREADY_BOOKED = "VEHICLE_ALREADY_BOOKED"

BUSY_REQUEST_STATUSES = ("BID - CONFIRMED", "REQUEST - CONFIRMED")
BUSY_BID_STATUSES = ("BID - CONFIRMED", "REQUEST - CONFIRMED")

_DEVICE_ID_MAX_LENGTH = 128
_PHONE_DIGITS = re.compile(r"\D+")


def normalize_phone(value: Optional[str]) -> str:
    if value is None:
        return ""
    return _PHONE_DIGITS.sub("", str(value).strip())


def normalize_device_id(raw: Optional[str]) -> Optional[str]:
    """Return a cleaned device id, or None when absent/invalid."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) > _DEVICE_ID_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_DEVICE_ID",
        )
    if any(ord(ch) < 32 for ch in text):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_DEVICE_ID",
        )
    return text


def booking_window(request_row: Request) -> tuple[date, date]:
    """Inclusive calendar window: pickUpDate .. coalesce(tripEndDate, pickUpDate)."""
    start = request_row.pickUpDate
    if isinstance(start, datetime):
        start = start.date()
    if not isinstance(start, date):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="INVALID_TRIP_WINDOW",
        )
    end = request_row.tripEndDate
    if isinstance(end, datetime):
        end = end.date()
    if end is None:
        end = start
    elif not isinstance(end, date):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="INVALID_TRIP_WINDOW",
        )
    if end < start:
        end = start
    return start, end


def windows_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


def _phone_set_for_user(user: Optional[User]) -> Set[str]:
    phones: Set[str] = set()
    if user is None:
        return phones
    primary = normalize_phone(getattr(user, "userAppId", None))
    if primary:
        phones.add(primary)
    alternate = normalize_phone(getattr(user, "alternateNumber", None))
    if alternate:
        phones.add(alternate)
    return phones


def _device_ids_for_user(db: Session, user_app_id: str) -> Set[str]:
    app_id = str(user_app_id or "").strip()
    if not app_id:
        return set()
    rows = (
        db.query(UserDevice.deviceId)
        .filter(UserDevice.userAppId == app_id)
        .all()
    )
    return {str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()}


def upsert_user_device(
    db: Session,
    user_app_id: str,
    device_id: Optional[str],
    *,
    flush: bool = True,
) -> None:
    """Bind deviceId to userAppId. No-op when device_id is empty/None."""
    cleaned = normalize_device_id(device_id)
    if cleaned is None:
        return
    app_id = str(user_app_id or "").strip()
    if not app_id:
        return

    existing = (
        db.query(UserDevice)
        .filter(
            UserDevice.userAppId == app_id,
            UserDevice.deviceId == cleaned,
        )
        .first()
    )
    now = datetime.now()
    if existing is not None:
        existing.lastSeenAt = now
    else:
        db.add(
            UserDevice(
                userAppId=app_id,
                deviceId=cleaned,
                lastSeenAt=now,
                createdAt=now,
            )
        )
    if flush:
        db.flush()


def assert_not_related_party(
    db: Session,
    customer_app_id: Optional[str],
    vendor_app_id: Optional[str],
) -> None:
    """Raise 403 RELATED_PARTY when customer and vendor share identity signals."""
    customer_id = str(customer_app_id or "").strip()
    vendor_id = str(vendor_app_id or "").strip()
    if not customer_id or not vendor_id:
        return

    if normalize_phone(customer_id) and normalize_phone(customer_id) == normalize_phone(
        vendor_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=DETAIL_RELATED_PARTY,
        )
    if customer_id == vendor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=DETAIL_RELATED_PARTY,
        )

    customer = db.query(User).filter(User.userAppId == customer_id).first()
    vendor = db.query(User).filter(User.userAppId == vendor_id).first()

    customer_phones = _phone_set_for_user(customer)
    # Always include the raw request/vendor ids even if user rows are missing.
    customer_phones |= {p for p in (normalize_phone(customer_id),) if p}
    vendor_phones = _phone_set_for_user(vendor)
    vendor_phones |= {p for p in (normalize_phone(vendor_id),) if p}

    if customer_phones & vendor_phones:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=DETAIL_RELATED_PARTY,
        )

    customer_devices = _device_ids_for_user(db, customer_id)
    vendor_devices = _device_ids_for_user(db, vendor_id)
    if customer_devices & vendor_devices:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=DETAIL_RELATED_PARTY,
        )


def assert_vehicle_available(
    db: Session,
    *,
    car_id: Optional[int],
    request_row: Request,
    exclude_rid: Optional[int] = None,
) -> None:
    """Raise 409 VEHICLE_ALREADY_BOOKED when CARID is busy on an overlapping trip."""
    if car_id is None:
        return
    try:
        car_key = int(car_id)
    except (TypeError, ValueError):
        return
    if car_key <= 0:
        return

    start, end = booking_window(request_row)
    rid = int(exclude_rid) if exclude_rid is not None else int(request_row.RID)

    candidates = (
        db.query(Request, BidDetail)
        .join(
            BidDetail,
            and_(
                BidDetail.rID == Request.RID,
                BidDetail.CARID == car_key,
                BidDetail.bidStatus.in_(BUSY_BID_STATUSES),
            ),
        )
        .filter(
            Request.requestStatus.in_(BUSY_REQUEST_STATUSES),
            Request.RID != rid,
        )
        .all()
    )

    for other_request, _bid in candidates:
        other_start, other_end = booking_window(other_request)
        if windows_overlap(start, end, other_start, other_end):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=DETAIL_VEHICLE_ALREADY_BOOKED,
            )


def related_party_phones(
    phones_a: Iterable[str], phones_b: Iterable[str]
) -> bool:
    """Test helper: digit-normalized phone set intersection."""
    a = {normalize_phone(p) for p in phones_a if normalize_phone(p)}
    b = {normalize_phone(p) for p in phones_b if normalize_phone(p)}
    return bool(a & b)
