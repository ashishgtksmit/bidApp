"""Event type registry for PR39."""

from __future__ import annotations

EVENT_BID_CREATED = "bid.created"
SCHEMA_VERSION_V1 = 1
AGGREGATE_REQUEST = "request"

# PR39 canary registry — expand in later waves only after approval.
SUPPORTED_EVENT_TYPES = frozenset({EVENT_BID_CREATED})
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION_V1})

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_PUBLISHED = "published"
OUTBOX_STATUS_DEAD = "dead"
OUTBOX_STATUSES = frozenset(
    {OUTBOX_STATUS_PENDING, OUTBOX_STATUS_PUBLISHED, OUTBOX_STATUS_DEAD}
)


def validate_event_type(event_type: str) -> None:
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError(f"unsupported eventType: {event_type}")


def validate_schema_version(version: int) -> None:
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schemaVersion: {version}")
