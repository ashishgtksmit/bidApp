"""Shared helpers for admin routes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, TypeVar

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy.orm import Query as SAQuery

T = TypeVar("T")

IST = timezone(timedelta(hours=5, minutes=30))

ST_OPEN = "BID - OPEN"
ST_HANDSHAKE = "BID - CONFIRMED"
ST_CONFIRMED = "REQUEST - CONFIRMED"
ST_REQ_CANCELLED = "REQUEST - CANCELLED BY USER"
ST_BOOKING_CANCELLED = "BOOKING - CANCELLED BY USER"
ALL_STATUSES = (ST_OPEN, ST_HANDSHAKE, ST_CONFIRMED, ST_REQ_CANCELLED, ST_BOOKING_CANCELLED)


def today_ist() -> date:
    return datetime.now(IST).date()


class Page(BaseModel):
    items: list[Any]
    total: int
    page: int
    pageSize: int


class PageParams:
    def __init__(
        self,
        page: int = Query(1, ge=1),
        pageSize: int = Query(25, ge=1, le=200),
    ):
        self.page = page
        self.page_size = pageSize

    def apply(self, q: SAQuery) -> tuple[list, int]:
        total = q.order_by(None).count()
        rows = q.offset((self.page - 1) * self.page_size).limit(self.page_size).all()
        return rows, total

    def wrap(self, items: list, total: int) -> dict:
        return {"items": items, "total": total, "page": self.page, "pageSize": self.page_size}


def mask_tail(value: Optional[str], keep: int = 4) -> Optional[str]:
    if not value:
        return None
    s = str(value)
    if len(s) <= keep:
        return "•" * len(s)
    return "•" * (len(s) - keep) + s[-keep:]


def to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        try:
            return v.isoformat()
        except ValueError:
            return None
    s = str(v)
    return None if s.startswith("0000") else s


def csv_ids(value: Optional[str]) -> list[int]:
    out: list[int] = []
    for part in (value or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def is_tombstone(user_app_id: Optional[str]) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()
