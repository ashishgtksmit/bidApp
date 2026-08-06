"""Transactional outbox writer (PR39/PR40).

Does not commit. The caller owns the business transaction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .models import DomainOutboxEvent
from .registry import (
    EVENT_BID_CREATED,
    EVENT_TYPE_FLAG_ENV,
    OUTBOX_STATUS_PENDING,
    SCHEMA_VERSION_V1,
    validate_event_type,
    validate_schema_version,
)
from .schemas import DomainEventEnvelopeV1

_logger = logging.getLogger("openbid.events.outbox")

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Metrics hooks (in-process counters; safe for unit tests / App Insights later).
_METRIC_OUTBOX_APPENDED: Dict[str, int] = defaultdict(int)
_METRIC_OUTBOX_SKIPPED_DISABLED: Dict[str, int] = defaultdict(int)
_METRIC_OUTBOX_APPEND_FAILED: Dict[str, int] = defaultdict(int)

# Backward-compatible PR39 aliases.
_METRIC_BID_CREATED_EMITTED = 0
_METRIC_BID_CREATED_SKIPPED_DISABLED = 0


def env_flag_enabled(name: str, *, default: str = "false") -> bool:
    """
    Centralized env flag parser.

    Defaults false. Never raises on malformed env. Accepts 1/true/yes/on only.
    """
    try:
        raw = os.getenv(name, default)
        if raw is None:
            return False
        return str(raw).strip().lower() in _TRUTHY
    except Exception:
        return False


def domain_events_enabled() -> bool:
    """Master switch. Default false (safe rollout)."""
    return env_flag_enabled("DOMAIN_EVENTS_ENABLED")


def bid_created_events_enabled() -> bool:
    """Canary switch for bid.created. Default false."""
    return env_flag_enabled("DOMAIN_EVENT_BID_CREATED_ENABLED")


def event_type_enabled(event_type: str) -> bool:
    """Per-event flag only (does not check master). Default false."""
    flag_name = EVENT_TYPE_FLAG_ENV.get(event_type)
    if not flag_name:
        return False
    return env_flag_enabled(flag_name)


def event_emission_enabled(event_type: str) -> bool:
    """
    Emission gate: DOMAIN_EVENTS_ENABLED AND the relevant per-event flag.

    Both default false. Unknown event types are never enabled via flags.
    """
    if not domain_events_enabled():
        return False
    return event_type_enabled(event_type)


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
    try:
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
    except Exception:
        _METRIC_OUTBOX_APPEND_FAILED[event_type] += 1
        _logger.warning(
            "outbox_append_failed eventType=%s metric_outbox_append_failed=%s",
            event_type,
            _METRIC_OUTBOX_APPEND_FAILED[event_type],
        )
        raise

    _METRIC_OUTBOX_APPENDED[event_type] += 1
    global _METRIC_BID_CREATED_EMITTED
    if event_type == EVENT_BID_CREATED:
        _METRIC_BID_CREATED_EMITTED += 1

    # Never log payload contents (may include bidderId candidate identifiers).
    _logger.info(
        "outbox_event_appended eventType=%s eventId=%s aggregateType=%s "
        "aggregateId=%s schemaVersion=%s metric_outbox_appended=%s",
        event_type,
        eid,
        aggregate_type,
        aggregate_id,
        schema_version,
        _METRIC_OUTBOX_APPENDED[event_type],
    )
    return row


def record_event_skipped_disabled(event_type: str) -> None:
    _METRIC_OUTBOX_SKIPPED_DISABLED[event_type] += 1
    global _METRIC_BID_CREATED_SKIPPED_DISABLED
    if event_type == EVENT_BID_CREATED:
        _METRIC_BID_CREATED_SKIPPED_DISABLED += 1
    # Default flags are off — avoid info-level spam on every mutation.
    _logger.debug(
        "outbox_skipped_disabled eventType=%s metric_outbox_skipped_disabled=%s",
        event_type,
        _METRIC_OUTBOX_SKIPPED_DISABLED[event_type],
    )


def record_bid_created_skipped_disabled() -> None:
    record_event_skipped_disabled(EVENT_BID_CREATED)


def maybe_append_domain_event(
    db: Session,
    *,
    event_type: str,
    aggregate_id: str,
    payload: Dict[str, Any],
    actor_auth_subject: Optional[str] = None,
    aggregate_type: str = "request",
) -> Optional[DomainOutboxEvent]:
    """
    Append outbox event when master + per-event flags are enabled.

    Returns the row when appended, None when skipped (flags off).
    Raises on append/flush failure so the caller can roll back.
    """
    if not event_emission_enabled(event_type):
        record_event_skipped_disabled(event_type)
        return None
    return append_outbox_event(
        db,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload=payload,
        actor_auth_subject=actor_auth_subject,
    )


def get_metrics() -> Dict[str, Any]:
    return {
        "bid_created_emitted": _METRIC_BID_CREATED_EMITTED,
        "bid_created_skipped_disabled": _METRIC_BID_CREATED_SKIPPED_DISABLED,
        "outbox_appended": dict(_METRIC_OUTBOX_APPENDED),
        "outbox_skipped_disabled": dict(_METRIC_OUTBOX_SKIPPED_DISABLED),
        "outbox_append_failed": dict(_METRIC_OUTBOX_APPEND_FAILED),
    }
