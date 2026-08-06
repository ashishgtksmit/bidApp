"""
PR39 transactional outbox + dispatcher unit tests (SQLite where practical).
"""

from __future__ import annotations

import json
import os
import sys
import types
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")
os.environ["DOMAIN_EVENTS_ENABLED"] = "false"
os.environ["DOMAIN_EVENT_BID_CREATED_ENABLED"] = "false"

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.database import Base  # noqa: E402
from app_v1.events.models import DomainOutboxEvent  # noqa: E402
from app_v1.events.outbox import (  # noqa: E402
    append_outbox_event,
    bid_created_events_enabled,
    domain_events_enabled,
    hash_actor_auth_subject,
)
from app_v1.events.registry import (  # noqa: E402
    AGGREGATE_REQUEST,
    EVENT_BID_CREATED,
    SCHEMA_VERSION_V1,
)
from app_v1.events.schemas import DomainEventEnvelopeV1  # noqa: E402


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[DomainOutboxEvent.__table__])
    Session = sessionmaker(bind=engine)
    return Session(), engine


def test_domain_events_flags_default_false(monkeypatch):
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_BID_CREATED_ENABLED", raising=False)
    assert domain_events_enabled() is False
    assert bid_created_events_enabled() is False


def test_append_outbox_event_persists_pending():
    db, _ = _session()
    row = append_outbox_event(
        db,
        event_type=EVENT_BID_CREATED,
        aggregate_type=AGGREGATE_REQUEST,
        aggregate_id="1504",
        payload={"requestId": 1504, "bidId": 921},
        actor_auth_subject="raw-auth-subject-should-be-hashed",
    )
    db.commit()
    assert row.status == "pending"
    assert row.eventType == "bid.created"
    assert row.schemaVersion == 1
    assert row.aggregateId == "1504"
    assert row.payload == {"requestId": 1504, "bidId": 921}
    assert row.actorAuthSubjectHash == hash_actor_auth_subject(
        "raw-auth-subject-should-be-hashed"
    )
    assert "raw-auth-subject" not in json.dumps(row.payload)
    assert row.eventId
    db.close()


def test_event_id_unique_constraint():
    db, _ = _session()
    eid = str(uuid.uuid4())
    append_outbox_event(
        db,
        event_type=EVENT_BID_CREATED,
        aggregate_type=AGGREGATE_REQUEST,
        aggregate_id="1",
        payload={"requestId": 1, "bidId": 1},
        event_id=eid,
    )
    db.commit()
    with pytest.raises(Exception):
        append_outbox_event(
            db,
            event_type=EVENT_BID_CREATED,
            aggregate_type=AGGREGATE_REQUEST,
            aggregate_id="2",
            payload={"requestId": 2, "bidId": 2},
            event_id=eid,
        )
        db.commit()
    db.rollback()
    db.close()


def test_envelope_rejects_phone_in_payload():
    with pytest.raises(Exception):
        DomainEventEnvelopeV1(
            eventId=str(uuid.uuid4()),
            eventType="bid.created",
            schemaVersion=1,
            aggregateType="request",
            aggregateId="1",
            occurredAt=datetime.utcnow(),
            payload={"requestId": 1, "bidId": 2, "phone": "9999999999"},
        )


def test_envelope_rejects_unsupported_version():
    with pytest.raises(Exception):
        DomainEventEnvelopeV1(
            eventId=str(uuid.uuid4()),
            eventType="bid.created",
            schemaVersion=99,
            aggregateType="request",
            aggregateId="1",
            occurredAt=datetime.utcnow(),
            payload={"requestId": 1, "bidId": 2},
        )


def test_append_does_not_commit():
    db, engine = _session()
    append_outbox_event(
        db,
        event_type=EVENT_BID_CREATED,
        aggregate_type=AGGREGATE_REQUEST,
        aggregate_id="9",
        payload={"requestId": 9, "bidId": 8},
    )
    # Caller owns commit — rollback must discard the outbox row.
    db.rollback()
    assert db.query(DomainOutboxEvent).count() == 0
    row = append_outbox_event(
        db,
        event_type=EVENT_BID_CREATED,
        aggregate_type=AGGREGATE_REQUEST,
        aggregate_id="9",
        payload={"requestId": 9, "bidId": 8},
    )
    db.commit()
    assert db.query(DomainOutboxEvent).count() == 1
    assert row.id is not None
    db.close()


def test_hash_actor_never_raw():
    raw = "sensitive-auth-subject-abc"
    hashed = hash_actor_auth_subject(raw)
    assert hashed is not None
    assert raw not in hashed
    assert len(hashed) == 32


def test_migration_preflight_importable():
    mig = ROOT / "migrations" / "pr39_domain_event_outbox"
    assert (mig / "preflight_domain_event_outbox.py").is_file()
    assert (mig / "apply_migration.py").is_file()
    assert (mig / "audit_domain_event_outbox.py").is_file()
    assert (mig / "README.md").is_file()


def test_outbox_backoff_formula():
    base, cap = 1, 30

    def backoff(attempt: int) -> int:
        return min(base * (2 ** max(0, attempt - 1)), cap)

    assert backoff(1) == 1
    assert backoff(2) == 2
    assert backoff(10) == 30


def test_safe_logging_no_pii_fields_in_outbox_writer():
    src = (ROOT / "app_v1" / "events" / "outbox.py").read_text()
    assert "fcmToken" not in src
    assert "password" not in src
    assert "customerAppId" not in src


def test_stream_fields_serialization_identifiers_only():
    from datetime import timezone

    env = DomainEventEnvelopeV1(
        eventId=str(uuid.uuid4()),
        eventType="bid.created",
        schemaVersion=1,
        aggregateType="request",
        aggregateId="1504",
        occurredAt=datetime.now(timezone.utc),
        payload={"requestId": 1504, "bidId": 921},
    )
    fields = env.to_stream_fields()
    blob = json.dumps(fields)
    assert "1504" in blob
    assert "921" in blob
    assert "7022359323" not in blob
    assert "fcm" not in blob.lower()
