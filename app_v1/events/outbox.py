"""Transactional outbox writer (PR39).

Does not commit. The caller owns the business transaction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .models import DomainOutboxEvent
from .registry import (
    EVENT_BID_CREATED,
    OUTBOX_STATUS_PENDING,
    SCHEMA_VERSION_V1,
    validate_event_type,
    validate_schema_version,
)
from .schemas import DomainEventEnvelopeV1

_logger = logging.getLogger("openbid.events.outbox")

# Metrics hooks (in-process counters; safe for unit tests / App Insights later).
_METRIC_BID_CREATED_EMITTED = 0
_METRIC_BID_CREATED_SKIPPED_DISABLED = 0


def domain_events_enabled() -> bool:
    """Master switch. Default false (safe rollout)."""
    return os.getenv("DOMAIN_EVENTS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def bid_created_events_enabled() -> bool:
    """Canary switch for bid.created. Default false."""
    return os.getenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def hash_actor_auth_subject(actor_auth_subject: Optional[str]) -> Optional[str]:
    """Hash auth subject for optional correlation. Never store raw subject."""
    if not actor_auth_subject:
        return None
    try:
        return hashlib.sha256(str(actor_auth_subject).encode("utf-8")).hexdigest()[:32]
    except Exception:
        # Do not block event emission if hashing fails.
        _logger.warning("actor_auth_subject_hash_unavailable")
        return None


def utc_now_naive() -> datetime:
    """UTC wall clock stored as naive DATETIME (event infrastructure uses UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def append_outbox_event(
    db: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Dict[str, Any],
    actor_auth_subject: Optional[str] = None,
    schema_version: int = SCHEMA_VERSION_V1,
    event_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> DomainOutboxEvent:
    """
    Add an outbox row to the current SQLAlchemy session/transaction.

    - Does not commit
    - Does not open another session
    - Validates event type / schema / payload
    - Raises on validation or flush failure so the business txn can roll back
    """
    validate_event_type(event_type)
    validate_schema_version(schema_version)

    eid = event_id or str(uuid.uuid4())
    occurred = occurred_at or utc_now_naive()
    if occurred.tzinfo is not None:
        occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)

    actor_hash = hash_actor_auth_subject(actor_auth_subject)

    # Validate via envelope (strict) before persistence.
    envelope = DomainEventEnvelopeV1(
        eventId=eid,
        eventType=event_type,
        schemaVersion=schema_version,
        aggregateType=aggregate_type,
        aggregateId=str(aggregate_id),
        occurredAt=occurred.replace(tzinfo=timezone.utc),
        actorAuthSubjectHash=actor_hash,
        payload=payload,
    )

    row = DomainOutboxEvent(
        eventId=envelope.eventId,
        eventType=envelope.eventType,
        aggregateType=envelope.aggregateType,
        aggregateId=envelope.aggregateId,
        payload=envelope.payload,
        schemaVersion=envelope.schemaVersion,
        occurredAt=occurred,
        createdAt=utc_now_naive(),
        publishedAt=None,
        attemptCount=0,
        nextAttemptAt=utc_now_naive(),
        lastErrorCode=None,
        lockedAt=None,
        lockedBy=None,
        status=OUTBOX_STATUS_PENDING,
        actorAuthSubjectHash=actor_hash,
    )
    db.add(row)
    # Flush so unique conflicts / JSON issues fail inside the caller's transaction.
    db.flush()

    global _METRIC_BID_CREATED_EMITTED
    if event_type == EVENT_BID_CREATED:
        _METRIC_BID_CREATED_EMITTED += 1
        _logger.info(
            "outbox_event_appended eventType=%s eventId=%s aggregateType=%s "
            "aggregateId=%s schemaVersion=%s metric_bid_created_emitted=%s",
            event_type,
            eid,
            aggregate_type,
            aggregate_id,
            schema_version,
            _METRIC_BID_CREATED_EMITTED,
        )
    else:
        _logger.info(
            "outbox_event_appended eventType=%s eventId=%s aggregateType=%s "
            "aggregateId=%s schemaVersion=%s",
            event_type,
            eid,
            aggregate_type,
            aggregate_id,
            schema_version,
        )
    return row


def record_bid_created_skipped_disabled() -> None:
    global _METRIC_BID_CREATED_SKIPPED_DISABLED
    _METRIC_BID_CREATED_SKIPPED_DISABLED += 1
    # Default flags are off — avoid info-level spam on every bid.
    _logger.debug(
        "bid_created_skipped reason=feature_disabled "
        "metric_bid_created_skipped_disabled=%s",
        _METRIC_BID_CREATED_SKIPPED_DISABLED,
    )


def get_metrics() -> Dict[str, int]:
    return {
        "bid_created_emitted": _METRIC_BID_CREATED_EMITTED,
        "bid_created_skipped_disabled": _METRIC_BID_CREATED_SKIPPED_DISABLED,
    }
