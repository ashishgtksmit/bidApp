"""SQLAlchemy model for openbid_domain_outbox (PR39)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.types import JSON

from ..database import Base


class DomainOutboxEvent(Base):
    """Transactional outbox row. Delivery metadata is mutable; business payload is not."""

    __tablename__ = "openbid_domain_outbox"
    __table_args__ = (
        UniqueConstraint("eventId", name="uq_openbid_domain_outbox_event_id"),
        Index("ix_outbox_status_next_attempt_id", "status", "nextAttemptAt", "id"),
        Index("ix_outbox_locked_at_status", "lockedAt", "status"),
        Index("ix_outbox_event_type_created", "eventType", "createdAt"),
        {"sqlite_autoincrement": True},
    )

    # BigInteger on MySQL; Integer variant for SQLite AUTOINCREMENT compatibility in tests.
    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    eventId = Column(String(36), nullable=False)
    eventType = Column(String(64), nullable=False)
    aggregateType = Column(String(32), nullable=False)
    aggregateId = Column(String(64), nullable=False)
    # JSON on MySQL; SQLite stores as TEXT via SQLAlchemy JSON type.
    payload = Column(JSON, nullable=False)
    schemaVersion = Column(Integer, nullable=False)
    occurredAt = Column(DateTime(timezone=False), nullable=False)
    createdAt = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )
    publishedAt = Column(DateTime(timezone=False), nullable=True)
    attemptCount = Column(Integer, nullable=False, default=0)
    nextAttemptAt = Column(DateTime(timezone=False), nullable=False)
    lastErrorCode = Column(String(64), nullable=True)
    lockedAt = Column(DateTime(timezone=False), nullable=True)
    lockedBy = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    # Optional actor hash (never raw authSubjectId). Stored separately from payload
    # so stream serialization can include it without polluting business payload.
    actorAuthSubjectHash = Column(String(64), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DomainOutboxEvent(id={self.id}, eventId={self.eventId!r}, "
            f"eventType={self.eventType!r}, status={self.status!r})>"
        )
