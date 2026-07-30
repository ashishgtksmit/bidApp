"""DB-backed rate limiting for public pre-login endpoints (PR5).

Uses shared MySQL storage so limits work across multi-instance Azure deployments.
Never log request bodies or secrets from callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.otp_challenge import ApiRateLimitBucket
from ..utils.common import ErrorResponse


def enforce_rate_limit(
    db: Session,
    *,
    bucket_key: str,
    max_hits: int,
    window_seconds: int,
) -> Optional[ErrorResponse]:
    """
    Increment a shared counter for (bucket_key, window).

    Returns ErrorResponse(message="RATE_LIMITED") when over limit, else None.
    Fail-open on unexpected DB errors so auth flows are not hard-blocked by limiter faults.
    """
    if max_hits <= 0 or window_seconds <= 0:
        return None

    window_epoch = int(datetime.now(timezone.utc).timestamp())
    window_start = datetime.fromtimestamp(
        (window_epoch // window_seconds) * window_seconds,
        tz=timezone.utc,
    ).replace(tzinfo=None)

    try:
        row = (
            db.query(ApiRateLimitBucket)
            .filter(
                ApiRateLimitBucket.bucket_key == bucket_key,
                ApiRateLimitBucket.window_start == window_start,
            )
            .with_for_update(read=False)
            .first()
        )
        if row is None:
            row = ApiRateLimitBucket(
                bucket_key=bucket_key,
                window_start=window_start,
                hit_count=1,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = (
                    db.query(ApiRateLimitBucket)
                    .filter(
                        ApiRateLimitBucket.bucket_key == bucket_key,
                        ApiRateLimitBucket.window_start == window_start,
                    )
                    .with_for_update(read=False)
                    .first()
                )
                if row is None:
                    return None
                row.hit_count = (row.hit_count or 0) + 1
                db.commit()
        else:
            row.hit_count = (row.hit_count or 0) + 1
            db.commit()

        if row.hit_count > max_hits:
            return ErrorResponse(message="RATE_LIMITED")
        return None
    except SQLAlchemyError:
        db.rollback()
        return None


def client_ip_from_request(request) -> str:
    """Best-effort client IP (X-Forwarded-For first hop, else request.client)."""
    forwarded = request.headers.get("x-forwarded-for") if request is not None else None
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request is not None and request.client is not None and request.client.host:
        return request.client.host
    return "unknown"
