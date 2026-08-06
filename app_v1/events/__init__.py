"""PR39 domain-event / transactional outbox helpers.

Domain events are invalidation signals. They are never accepted from mobile clients.
"""

from .outbox import append_outbox_event, domain_events_enabled, bid_created_events_enabled
from .registry import EVENT_BID_CREATED, SCHEMA_VERSION_V1

__all__ = [
    "append_outbox_event",
    "domain_events_enabled",
    "bid_created_events_enabled",
    "EVENT_BID_CREATED",
    "SCHEMA_VERSION_V1",
]
