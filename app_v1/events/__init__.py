"""PR39/PR40 domain-event / transactional outbox helpers.

Domain events are invalidation signals. They are never accepted from mobile clients.
"""

from .outbox import (
    append_outbox_event,
    bid_created_events_enabled,
    domain_events_enabled,
    event_emission_enabled,
    event_type_enabled,
    maybe_append_domain_event,
    record_bid_created_skipped_disabled,
    record_event_skipped_disabled,
)
from .registry import (
    EVENT_BID_ACCEPTED,
    EVENT_BID_CREATED,
    EVENT_BID_DELETED,
    EVENT_BID_UPDATED,
    EVENT_BOOKING_CANCELLED_BY_CUSTOMER,
    EVENT_DRIVER_ASSIGNMENT_CHANGED,
    EVENT_HANDSHAKE_ACCEPTED,
    EVENT_HANDSHAKE_CANCELLED,
    EVENT_HANDSHAKE_REJECTED,
    SCHEMA_VERSION_V1,
)

__all__ = [
    "append_outbox_event",
    "maybe_append_domain_event",
    "domain_events_enabled",
    "bid_created_events_enabled",
    "event_emission_enabled",
    "event_type_enabled",
    "record_bid_created_skipped_disabled",
    "record_event_skipped_disabled",
    "EVENT_BID_CREATED",
    "EVENT_BID_UPDATED",
    "EVENT_BID_DELETED",
    "EVENT_BID_ACCEPTED",
    "EVENT_HANDSHAKE_CANCELLED",
    "EVENT_HANDSHAKE_ACCEPTED",
    "EVENT_HANDSHAKE_REJECTED",
    "EVENT_BOOKING_CANCELLED_BY_CUSTOMER",
    "EVENT_DRIVER_ASSIGNMENT_CHANGED",
    "SCHEMA_VERSION_V1",
]
