"""Domain event envelope schemas (v1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .registry import (
    AGGREGATE_REQUEST,
    EVENT_BID_ACCEPTED,
    EVENT_BID_CREATED,
    EVENT_BID_DELETED,
    EVENT_BID_UPDATED,
    EVENT_BOOKING_CANCELLED_BY_CUSTOMER,
    EVENT_DRIVER_ASSIGNMENT_CHANGED,
    EVENT_HANDSHAKE_ACCEPTED,
    EVENT_HANDSHAKE_CANCELLED,
    EVENT_HANDSHAKE_REJECTED,
    EVENT_REQUEST_CREATED,
    EVENT_REQUEST_UPDATED,
    EVENT_REQUEST_CANCELLED,
    EVENT_REQUEST_REOPENED,
    SCHEMA_VERSION_V1,
    validate_event_type,
)


class BidCreatedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)


class BidUpdatedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)


class BidDeletedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)
    bidderId: str = Field(..., min_length=1, max_length=64)


class BidAcceptedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)


class HandshakeCancelledPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)


class HandshakeAcceptedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)


class HandshakeRejectedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    bidId: int = Field(..., gt=0)
    bidderId: str = Field(..., min_length=1, max_length=64)


class BookingCancelledByCustomerPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)


class DriverAssignmentChangedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)
    driverId: int = Field(..., gt=0)


class RequestCreatedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)


class RequestUpdatedPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)


class RequestCancelledPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)


class RequestReopenedPayloadV1(BaseModel):
    """PR46 — identifier-only; aggregateId/requestId are the NEW RID."""

    model_config = ConfigDict(extra="forbid")

    requestId: int = Field(..., gt=0)


PAYLOAD_MODELS: Dict[str, Type[BaseModel]] = {
    EVENT_BID_CREATED: BidCreatedPayloadV1,
    EVENT_BID_UPDATED: BidUpdatedPayloadV1,
    EVENT_BID_DELETED: BidDeletedPayloadV1,
    EVENT_BID_ACCEPTED: BidAcceptedPayloadV1,
    EVENT_HANDSHAKE_CANCELLED: HandshakeCancelledPayloadV1,
    EVENT_HANDSHAKE_ACCEPTED: HandshakeAcceptedPayloadV1,
    EVENT_HANDSHAKE_REJECTED: HandshakeRejectedPayloadV1,
    EVENT_BOOKING_CANCELLED_BY_CUSTOMER: BookingCancelledByCustomerPayloadV1,
    EVENT_DRIVER_ASSIGNMENT_CHANGED: DriverAssignmentChangedPayloadV1,
    EVENT_REQUEST_CREATED: RequestCreatedPayloadV1,
    EVENT_REQUEST_UPDATED: RequestUpdatedPayloadV1,
    EVENT_REQUEST_CANCELLED: RequestCancelledPayloadV1,
    EVENT_REQUEST_REOPENED: RequestReopenedPayloadV1,
}


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
        model = PAYLOAD_MODELS.get(event_type) if event_type else None
        if model is None:
            raise ValueError(f"unsupported eventType for payload: {event_type}")
        return model.model_validate(value).model_dump()

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
