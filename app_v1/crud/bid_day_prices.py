"""PR59 per-day prices for sightseeing and (PR60) tour bids.

biddetails.bidAmount stays the total; bid_day_prices holds the optional
breakdown and biddetails.pricingMode records PER_DAY / TOTAL. Only called for
sightseeing / tour requests, so transfer bids never touch either object.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..models.bid_day_prices import BidDayPrice
from ..models.request_table import Request
from ..models.sightseeing import RequestItineraryDay
from .sightseeing import is_itinerary_type

PRICING_PER_DAY = "PER_DAY"
PRICING_TOTAL = "TOTAL"
ERR_INVALID_DAY_PRICES = "INVALID_DAY_PRICES"
_TOLERANCE = Decimal("0.01")


class DayPriceError(Exception):
    code = ERR_INVALID_DAY_PRICES


def _reject(detail: str) -> DayPriceError:
    return DayPriceError(detail)


def is_itinerary_request(db: Session, request_row: Request) -> bool:
    """Sightseeing or tour request (the types that can carry per-day prices)."""
    return is_itinerary_type(db, getattr(request_row, "requestType", None))


def _itinerary_day_count(db: Session, rid: int) -> int:
    return int(
        db.query(func.count(RequestItineraryDay.dayId))
        .filter(RequestItineraryDay.requestId == rid)
        .scalar()
        or 0
    )


def validate_day_prices(
    db: Session,
    request_row: Request,
    total: float,
    day_prices: Optional[Sequence[float]],
) -> Optional[List[Decimal]]:
    """Return the per-day amounts to store, or None for a one-total bid.

    Raises DayPriceError when a breakdown is sent for a transfer, the day count
    does not match the itinerary, or the days do not add up to bidAmount.
    """
    if not day_prices:
        return None
    if not is_itinerary_request(db, request_row):
        raise _reject("Per-day prices are only allowed for sightseeing or tour requests")
    days = _itinerary_day_count(db, int(request_row.RID))
    if days < 1 or len(day_prices) != days:
        raise _reject(f"Enter a price for each of the {days} days")
    amounts = [Decimal(str(v)).quantize(Decimal("0.01")) for v in day_prices]
    if abs(sum(amounts) - Decimal(str(total))) > _TOLERANCE:
        raise _reject("Per-day prices must add up to the total bid amount")
    return amounts


def save_day_prices(db: Session, bid_id: int, amounts: Optional[Sequence[Decimal]]) -> None:
    """Replace a sightseeing bid's breakdown and pricingMode (caller owns the transaction)."""
    db.query(BidDayPrice).filter(BidDayPrice.bidId == bid_id).delete(synchronize_session=False)
    for day_no, amount in enumerate(amounts or [], start=1):
        db.add(BidDayPrice(bidId=bid_id, dayNo=day_no, amount=amount))
    db.execute(
        text("UPDATE biddetails SET pricingMode = :mode WHERE BID = :bid"),
        {"mode": PRICING_PER_DAY if amounts else PRICING_TOTAL, "bid": bid_id},
    )
    db.flush()


def delete_day_prices(db: Session, bid_id: int) -> None:
    db.query(BidDayPrice).filter(BidDayPrice.bidId == bid_id).delete(synchronize_session=False)


def load_day_prices(db: Session, bid_ids: Iterable[int]) -> Dict[int, List[float]]:
    """BID -> amounts ordered by day; bids priced as one total are absent."""
    ids = sorted({int(b) for b in bid_ids if b is not None})
    if not ids:
        return {}
    rows = (
        db.query(BidDayPrice.bidId, BidDayPrice.amount)
        .filter(BidDayPrice.bidId.in_(ids))
        .order_by(BidDayPrice.bidId, BidDayPrice.dayNo)
        .all()
    )
    result: Dict[int, List[float]] = {}
    for bid_id, amount in rows:
        result.setdefault(int(bid_id), []).append(float(amount))
    return result
