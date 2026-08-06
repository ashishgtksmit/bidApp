"""Domain event envelope schemas (v1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .registry import (
    AGGREGATE_REQUEST,
    EVENT_BID_CREATED,
    SCHEMA_VERSION_V1,
    validate_event_type,
)


class BidCreatedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)


class DomainEventEnvelopeV1(BaseModel):
    """Strict v1 envelope. Extra top-level fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    eventId: str = Field(..., min_length=36, max_length=36)
    eventType: str
    schemaVersion: int
    aggregateType: str
    aggregateId: str = Field(..., min_length=1, max_length=64)
    occurredAt: datetime
    actorAuthSubjectHash: Optional[str] = Field(default=None, max_length=64)
    payload: Dict[str, Any]

    @field_validator("eventType")
    @classmethod
    def _event_type_known(cls, value: str) -> str:
        validate_event_type(value)
        return value

    @field_validator("schemaVersion")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION_V1:
            raise ValueError(f"unsupported schemaVersion: {value}")
        return value

    @field_validator("aggregateType")
    @classmethod
    def _aggregate_type(cls, value: str) -> str:
        if value not in {AGGREGATE_REQUEST}:
            raise ValueError(f"unsupported aggregateType: {value}")
        return value

    @field_validator("payload")
    @classmethod
    def _payload_for_type(cls, value: Dict[str, Any], info) -> Dict[str, Any]:
        event_type = (info.data or {}).get("eventType")
        if event_type == EVENT_BID_CREATED:
            return BidCreatedPayloadV1.model_validate(value).model_dump()
        raise ValueError(f"unsupported eventType for payload: {event_type}")

    def to_stream_fields(self) -> Dict[str, str]:
        """Serialize for Redis Stream XADD (string fields only)."""
        import json

        fields = {
            "eventId": self.eventId,
            "eventType": self.eventType,
            "schemaVersion": str(self.schemaVersion),
            "aggregateType": self.aggregateType,
            "aggregateId": self.aggregateId,
            "occurredAt": self.occurredAt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "payload": json.dumps(self.payload, separators=(",", ":")),
        }
        if self.actorAuthSubjectHash:
            fields["actorAuthSubjectHash"] = self.actorAuthSubjectHash
        return fields
