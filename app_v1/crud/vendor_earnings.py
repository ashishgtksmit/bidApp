"""PR22 vendor earnings reporting CRUD.

Gross completed booking value for JWT-owned past REQUEST - CONFIRMED trips.
No bid/review/customer joins. No paymentStatus filter. No vendor approval gate.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.request_table import Request
from ..schemas.vendor_earnings import (
    VendorEarningTripItem,
    VendorEarningsBucket,
    VendorEarningsReport,
    VendorEarningsSummary,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
CONFIRMED_STATUS = "REQUEST - CONFIRMED"
CURRENCY = "INR"
MAX_RANGE_MONTHS = 24
TRIP_LIMIT = 10
DEFAULT_BUCKET_MONTHS = 6

_MONTH_ABBREV = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _today_ist() -> date:
    return datetime.now(IST).date()


def _earnings_pickup_datetime(req: Request) -> datetime:
    """Combine pickup date/time as Asia/Kolkata wall clock (naive)."""
    rid = getattr(req, "RID", None)
    pickup_date = getattr(req, "pickUpDate", None)
    pickup_time = getattr(req, "pickUpTime", None)
    if not isinstance(pickup_date, date):
        logger.error("REPORT_DATA_INVALID rid=%s reason=pickup_date_type", rid)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="REPORT_QUERY_FAILED",
        )
    if not isinstance(pickup_time, time):
        logger.error("REPORT_DATA_INVALID rid=%s reason=pickup_time_type", rid)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="REPORT_QUERY_FAILED",
        )
    try:
        d = pickup_date.date() if isinstance(pickup_date, datetime) else pickup_date
        return datetime.combine(d, pickup_time)
    except (TypeError, ValueError) as exc:
        logger.error("REPORT_DATA_INVALID rid=%s reason=pickup_combine", rid)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="REPORT_QUERY_FAILED",
        ) from exc


def _parse_report_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid {field_name} format. Expected yyyy-MM-dd.",
        ) from exc


def _calendar_months_spanned(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _month_label(year: int, month: int) -> str:
    return f"{_MONTH_ABBREV[month - 1]} {year}"


def _month_bounds(year: int, month: int) -> Tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _iter_months(start: date, end: date) -> List[Tuple[int, int]]:
    """Ascending list of (year, month) intersecting inclusive date range."""
    months: List[Tuple[int, int]] = []
    y, m = start.year, start.month
    end_y, end_m = end.year, end.month
    while (y, m) <= (end_y, end_m):
        months.append((y, m))
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return months


def _last_n_months_including_current(n: int, today: date) -> List[Tuple[int, int]]:
    months: List[Tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append((y, m))
        if m == 1:
            y -= 1
            m = 12
        else:
            m -= 1
    months.reverse()
    return months


def _non_negative_amount(req: Request) -> Optional[int]:
    """Return non-negative int amount, or None to exclude the row."""
    raw = getattr(req, "finalAmount", None)
    if raw is None:
        return 0
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "REPORT_NEGATIVE_OR_INVALID_AMOUNT rid=%s",
            getattr(req, "RID", None),
        )
        return None
    if amount < 0:
        logger.warning(
            "REPORT_NEGATIVE_AMOUNT_EXCLUDED rid=%s",
            getattr(req, "RID", None),
        )
        return None
    return amount


def _empty_report(
    *,
    period_start: Optional[str],
    period_end: Optional[str],
    monthly_buckets: List[VendorEarningsBucket],
) -> VendorEarningsReport:
    return VendorEarningsReport(
        periodStart=period_start,
        periodEnd=period_end,
        summary=VendorEarningsSummary(
            completedTripCount=0,
            grossBookingValue=0,
            currency=CURRENCY,
        ),
        monthlyBuckets=monthly_buckets,
        trips=[],
    )


def get_vendor_earnings_report(
    db: Session,
    user_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> VendorEarningsReport:
    """
    Server-authoritative vendor earnings report.

    Ownership: Request.requestWonBy == JWT sub.
    Eligibility: REQUEST - CONFIRMED + past Asia/Kolkata pickup.
    Metric: gross completed booking value from finalAmount (integer rupees).
    """
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    has_start = start_date is not None and str(start_date).strip() != ""
    has_end = end_date is not None and str(end_date).strip() != ""

    if has_start ^ has_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="startDate and endDate must both be supplied or both omitted.",
        )

    range_start: Optional[date] = None
    range_end: Optional[date] = None
    if has_start and has_end:
        range_start = _parse_report_date(str(start_date).strip(), "startDate")
        range_end = _parse_report_date(str(end_date).strip(), "endDate")
        if range_start > range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="startDate must be less than or equal to endDate.",
            )
        if _calendar_months_spanned(range_start, range_end) > MAX_RANGE_MONTHS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="REPORT_RANGE_TOO_LARGE",
            )

    today = _today_ist()
    now_ist = _now_ist_naive()

    if range_start is None:
        period_start_str: Optional[str] = None
        period_end_str: Optional[str] = today.isoformat()
        month_keys = _last_n_months_including_current(DEFAULT_BUCKET_MONTHS, today)
    else:
        assert range_end is not None
        period_start_str = range_start.isoformat()
        period_end_str = range_end.isoformat()
        month_keys = _iter_months(range_start, range_end)

    try:
        rows = (
            db.query(Request)
            .filter(
                Request.requestWonBy == user_id,
                Request.requestStatus == CONFIRMED_STATUS,
            )
            .all()
        )
    except SQLAlchemyError:
        logger.exception("REPORT_QUERY_FAILED")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="REPORT_QUERY_FAILED",
        ) from None

    eligible: List[Tuple[datetime, int, int, Request]] = []
    for req in rows:
        if getattr(req, "requestWonBy", None) is None:
            continue
        pickup_dt = _earnings_pickup_datetime(req)
        if pickup_dt >= now_ist:
            continue
        pickup_day = pickup_dt.date()
        if range_start is not None and range_end is not None:
            if pickup_day < range_start or pickup_day > range_end:
                continue
        amount = _non_negative_amount(req)
        if amount is None:
            continue
        rid = int(req.RID)
        eligible.append((pickup_dt, rid, amount, req))

    if not eligible and not month_keys:
        return _empty_report(
            period_start=period_start_str,
            period_end=period_end_str,
            monthly_buckets=[],
        )

    trip_count = len(eligible)
    gross_total = sum(amount for _, _, amount, _ in eligible)

    bucket_totals = {(y, m): [0, 0] for y, m in month_keys}
    for pickup_dt, _rid, amount, _req in eligible:
        key = (pickup_dt.year, pickup_dt.month)
        if key in bucket_totals:
            bucket_totals[key][0] += 1
            bucket_totals[key][1] += amount

    monthly_buckets: List[VendorEarningsBucket] = []
    for year, month in month_keys:
        start, end = _month_bounds(year, month)
        count, value = bucket_totals.get((year, month), [0, 0])
        monthly_buckets.append(
            VendorEarningsBucket(
                periodStart=start,
                periodEnd=end,
                label=_month_label(year, month),
                completedTripCount=count,
                grossBookingValue=value,
            )
        )

    eligible.sort(key=lambda row: (row[0], row[1]), reverse=True)
    trip_rows = eligible[:TRIP_LIMIT]
    trips: List[VendorEarningTripItem] = []
    for pickup_dt, rid, amount, req in trip_rows:
        pickup_time = getattr(req, "pickUpTime", None)
        if isinstance(pickup_time, time):
            time_str = pickup_time.strftime("%H:%M:%S")
        else:
            time_str = pickup_dt.strftime("%H:%M:%S")
        trips.append(
            VendorEarningTripItem(
                requestId=rid,
                pickupDate=pickup_dt.date().isoformat(),
                pickupTime=time_str,
                fromLocation=str(getattr(req, "fromLocation", "") or ""),
                toLocation=str(getattr(req, "toLocation", "") or ""),
                grossAmount=amount,
                requestStatus=str(getattr(req, "requestStatus", CONFIRMED_STATUS)),
            )
        )

    return VendorEarningsReport(
        periodStart=period_start_str,
        periodEnd=period_end_str,
        summary=VendorEarningsSummary(
            completedTripCount=trip_count,
            grossBookingValue=gross_total,
            currency=CURRENCY,
        ),
        monthlyBuckets=monthly_buckets,
        trips=trips,
    )
