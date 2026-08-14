"""Transactional outbox writer (PR39/PR40).

Does not commit. The caller owns the business transaction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .models import DomainOutboxEvent
from .registry import (
    EVENT_BID_CREATED,
    EVENT_BOOKING_CANCELLED_BY_CUSTOMER,
    EVENT_DRIVER_ASSIGNMENT_CHANGED,
    EVENT_HANDSHAKE_ACCEPTED,
    EVENT_HANDSHAKE_CANCELLED,
    EVENT_HANDSHAKE_REJECTED,
    EVENT_REQUEST_CREATED,
    EVENT_REQUEST_UPDATED,
    EVENT_REQUEST_CANCELLED,
    EVENT_REQUEST_REOPENED,
    EVENT_TYPE_FLAG_ENV,
    OUTBOX_STATUS_PENDING,
    SCHEMA_VERSION_V1,
    validate_event_type,
    validate_schema_version,
)
from .schemas import DomainEventEnvelopeV1

_logger = logging.getLogger("openbid.events.outbox")

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY_EXPLICIT = frozenset({"0", "false", "no", "off", ""})

# Metrics hooks (in-process counters; safe for unit tests / App Insights later).
_METRIC_OUTBOX_APPENDED: Dict[str, int] = defaultdict(int)
_METRIC_OUTBOX_SKIPPED_DISABLED: Dict[str, int] = defaultdict(int)
_METRIC_OUTBOX_APPEND_FAILED: Dict[str, int] = defaultdict(int)
_METRIC_FLAG_MALFORMED: Dict[str, int] = defaultdict(int)

# Backward-compatible PR39 aliases.
_METRIC_BID_CREATED_EMITTED = 0
_METRIC_BID_CREATED_SKIPPED_DISABLED = 0

# Configuration source categories (sanitized; never dump env values).
CONFIG_SOURCE_PROCESS_ENV = "azure_or_process_environment"
CONFIG_SOURCE_DEFAULT = "default"
CONFIG_SOURCE_TEST_OVERRIDE = "test_override"


def env_flag_raw(name: str) -> Optional[str]:
    """Return raw env string or None when unset."""
    return os.environ.get(name)


def env_flag_enabled(name: str, *, default: str = "false") -> bool:
    """
    Centralized env flag parser.

    Accepted true (case-insensitive, trimmed): 1, true, yes, on.
    Everything else fails closed (false), including unset, empty, and malformed.
    Never raises. Malformed values increment a safe warning metric.
    """
    try:
        if name not in os.environ:
            raw = default
            present = False
        else:
            raw = os.environ.get(name)
            present = True
        if raw is None:
            return False
        normalized = str(raw).strip().lower()
        if normalized in _TRUTHY:
            return True
        if present and normalized not in _FALSY_EXPLICIT:
            _METRIC_FLAG_MALFORMED[name] += 1
            _logger.warning(
                "domain_event_flag_malformed flag=%s fail_closed=true "
                "metric_flag_malformed=%s",
                name,
                _METRIC_FLAG_MALFORMED[name],
            )
        return False
    except Exception:
        return False


def env_flag_source_category(name: str) -> str:
    """
    Classify where the effective flag value comes from.

    Does not report raw values. Packaged dotenv must not override process env
    (load_dotenv override=False); when the key is present in os.environ it is
    treated as Azure/process environment (or a test monkeypatch).
    """
    if name in os.environ:
        # Tests commonly monkeypatch os.environ; production App Settings land here.
        return CONFIG_SOURCE_PROCESS_ENV
    return CONFIG_SOURCE_DEFAULT


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

    Both default false. Master alone never enables an event.
    Unknown event types are never enabled via flags.
    """
    if not domain_events_enabled():
        return False
    return event_type_enabled(event_type)


def deployment_revision_hint() -> str:
    """Best-effort sanitized revision identity (no secrets)."""
    for key in (
        "OPENBID_DEPLOY_REVISION",
        "GITHUB_SHA",
        "WEBSITE_DEPLOYMENT_ID",
        "SCM_COMMIT_ID",
    ):
        val = os.environ.get(key)
        if val:
            return str(val).strip()[:40]
    return "unknown"


def instance_id_hash() -> str:
    """Short hash of instance id for multi-instance correlation (not raw id)."""
    raw = (
        os.environ.get("WEBSITE_INSTANCE_ID")
        or os.environ.get("HOSTNAME")
        or "local"
    )
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]


def flag_snapshot_booleans() -> Dict[str, Any]:
    """Sanitized boolean snapshot of master + registered per-event flags."""
    per_event = {
        event_type: bool(env_flag_enabled(flag_name))
        for event_type, flag_name in EVENT_TYPE_FLAG_ENV.items()
    }
    return {
        "DOMAIN_EVENTS_ENABLED": bool(domain_events_enabled()),
        "per_event": per_event,
        "DOMAIN_EVENTS_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENTS_ENABLED"
        ),
        "DOMAIN_EVENT_BID_CREATED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_BID_CREATED_ENABLED"
        ),
        "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED"
        ),
        "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED"
        ),
        "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED"
        ),
        "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED"
        ),
        "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED"
        ),
        "DOMAIN_EVENT_REQUEST_CREATED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_REQUEST_CREATED_ENABLED"
        ),
        "DOMAIN_EVENT_REQUEST_UPDATED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_REQUEST_UPDATED_ENABLED"
        ),
        "DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED"
        ),
        "DOMAIN_EVENT_REQUEST_REOPENED_ENABLED_source": env_flag_source_category(
            "DOMAIN_EVENT_REQUEST_REOPENED_ENABLED"
        ),
    }


def process_bound_flag_snapshot(*, reason: str = "http") -> Dict[str, Any]:
    """
    Process-bound flag proof payload (no secrets, no account/RID identifiers).

    Used by startup logs and authenticated ops GET /domain-event-flag-snapshot.
    """
    snap = flag_snapshot_booleans()
    per = snap["per_event"]
    return {
        "reason": reason,
        "deployRevision": deployment_revision_hint(),
        "instanceHash": instance_id_hash(),
        "DOMAIN_EVENTS_ENABLED": snap["DOMAIN_EVENTS_ENABLED"],
        "DOMAIN_EVENTS_ENABLED_source": snap["DOMAIN_EVENTS_ENABLED_source"],
        "perEvent": dict(per),
        "eventFlagEnv": dict(EVENT_TYPE_FLAG_ENV),
        "handshakeCancelled": {
            "eventType": EVENT_HANDSHAKE_CANCELLED,
            "envFlag": "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_HANDSHAKE_CANCELLED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_HANDSHAKE_CANCELLED, False)
            ),
            "source": snap["DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED_source"],
        },
        "handshakeAccepted": {
            "eventType": EVENT_HANDSHAKE_ACCEPTED,
            "envFlag": "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_HANDSHAKE_ACCEPTED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_HANDSHAKE_ACCEPTED, False)
            ),
            "source": snap["DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED_source"],
        },
        "handshakeRejected": {
            "eventType": EVENT_HANDSHAKE_REJECTED,
            "envFlag": "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_HANDSHAKE_REJECTED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_HANDSHAKE_REJECTED, False)
            ),
            "source": snap["DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED_source"],
        },
        "bookingCancelledByCustomer": {
            "eventType": EVENT_BOOKING_CANCELLED_BY_CUSTOMER,
            "envFlag": "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED",
            "perEventEnabled": bool(
                per.get(EVENT_BOOKING_CANCELLED_BY_CUSTOMER, False)
            ),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_BOOKING_CANCELLED_BY_CUSTOMER, False)
            ),
            "source": snap[
                "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED_source"
            ],
        },
        "driverAssignmentChanged": {
            "eventType": EVENT_DRIVER_ASSIGNMENT_CHANGED,
            "envFlag": "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED",
            "perEventEnabled": bool(
                per.get(EVENT_DRIVER_ASSIGNMENT_CHANGED, False)
            ),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_DRIVER_ASSIGNMENT_CHANGED, False)
            ),
            "source": snap[
                "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED_source"
            ],
        },
        "requestCreated": {
            "eventType": EVENT_REQUEST_CREATED,
            "envFlag": "DOMAIN_EVENT_REQUEST_CREATED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_REQUEST_CREATED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_REQUEST_CREATED, False)
            ),
            "source": snap["DOMAIN_EVENT_REQUEST_CREATED_ENABLED_source"],
        },
        "requestUpdated": {
            "eventType": EVENT_REQUEST_UPDATED,
            "envFlag": "DOMAIN_EVENT_REQUEST_UPDATED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_REQUEST_UPDATED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_REQUEST_UPDATED, False)
            ),
            "source": snap["DOMAIN_EVENT_REQUEST_UPDATED_ENABLED_source"],
        },
        "requestCancelled": {
            "eventType": EVENT_REQUEST_CANCELLED,
            "envFlag": "DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_REQUEST_CANCELLED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_REQUEST_CANCELLED, False)
            ),
            "source": snap["DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED_source"],
        },
        "requestReopened": {
            "eventType": EVENT_REQUEST_REOPENED,
            "envFlag": "DOMAIN_EVENT_REQUEST_REOPENED_ENABLED",
            "perEventEnabled": bool(per.get(EVENT_REQUEST_REOPENED, False)),
            "emissionEnabled": bool(
                snap["DOMAIN_EVENTS_ENABLED"]
                and per.get(EVENT_REQUEST_REOPENED, False)
            ),
            "source": snap["DOMAIN_EVENT_REQUEST_REOPENED_ENABLED_source"],
        },
    }


def configure_domain_event_logging() -> None:
    """
    Ensure openbid.events.* INFO logs reach process stdout.

    Azure App Service captures stdout; without a handler, stdlib logging
    discards INFO (lastResort is WARNING+), so domain_event_flag_snapshot
    and emission decisions were invisible to ops downloads.
    """
    try:
        root_name = "openbid.events"
        logger = logging.getLogger(root_name)
        if any(
            isinstance(h, logging.StreamHandler) and getattr(h, "_openbid_events", False)
            for h in logger.handlers
        ):
            return
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s %(message)s")
        )
        handler._openbid_events = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Avoid duplicate emission through root if root later gains handlers.
        logger.propagate = False
    except Exception:
        # Never block API boot on logging setup.
        pass


def log_domain_event_flag_snapshot(*, reason: str = "startup") -> None:
    """
    Safe structured log of interpreted domain-event flags.

    Never logs secrets, raw account ids, or full environ dumps.
    Includes every registered per-event boolean so Wave B2/B3/B4/C1/C2 binding is provable.
    """
    payload = process_bound_flag_snapshot(reason=reason)
    per = payload["perEvent"]
    # Compact eventType=bool pairs for log search (includes handshake.* + booking/driver).
    per_pairs = " ".join(
        f"{et}={str(bool(enabled)).lower()}" for et, enabled in sorted(per.items())
    )
    hc = payload["handshakeCancelled"]
    ha = payload["handshakeAccepted"]
    hr = payload["handshakeRejected"]
    bc = payload["bookingCancelledByCustomer"]
    da = payload["driverAssignmentChanged"]
    rc = payload["requestCreated"]
    ru = payload["requestUpdated"]
    rx = payload["requestCancelled"]
    rr = payload["requestReopened"]
    msg = (
        "domain_event_flag_snapshot reason=%s revision=%s instance_hash=%s "
        "DOMAIN_EVENTS_ENABLED=%s DOMAIN_EVENT_BID_CREATED_ENABLED=%s "
        "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED=%s "
        "handshake.cancelled=%s handshake_cancelled_emission=%s "
        "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED=%s "
        "handshake.accepted=%s handshake_accepted_emission=%s "
        "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED=%s "
        "handshake.rejected=%s handshake_rejected_emission=%s "
        "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED=%s "
        "booking.cancelled_by_customer=%s booking_cancelled_by_customer_emission=%s "
        "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED=%s "
        "driver.assignment_changed=%s driver_assignment_changed_emission=%s "
        "DOMAIN_EVENT_REQUEST_CREATED_ENABLED=%s "
        "request.created=%s request_created_emission=%s "
        "DOMAIN_EVENT_REQUEST_UPDATED_ENABLED=%s "
        "request.updated=%s request_updated_emission=%s "
        "DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED=%s "
        "request.cancelled=%s request_cancelled_emission=%s "
        "DOMAIN_EVENT_REQUEST_REOPENED_ENABLED=%s "
        "request.reopened=%s request_reopened_emission=%s "
        "master_source=%s bid_created_source=%s handshake_cancelled_source=%s "
        "handshake_accepted_source=%s handshake_rejected_source=%s "
        "booking_cancelled_by_customer_source=%s "
        "driver_assignment_changed_source=%s "
        "request_created_source=%s "
        "request_updated_source=%s "
        "request_cancelled_source=%s "
        "request_reopened_source=%s "
        "pr40_any_enabled=%s per_event=%s"
    )
    args = (
        reason,
        payload["deployRevision"],
        payload["instanceHash"],
        payload["DOMAIN_EVENTS_ENABLED"],
        per.get(EVENT_BID_CREATED, False),
        per.get(EVENT_HANDSHAKE_CANCELLED, False),
        hc["perEventEnabled"],
        hc["emissionEnabled"],
        per.get(EVENT_HANDSHAKE_ACCEPTED, False),
        ha["perEventEnabled"],
        ha["emissionEnabled"],
        per.get(EVENT_HANDSHAKE_REJECTED, False),
        hr["perEventEnabled"],
        hr["emissionEnabled"],
        per.get(EVENT_BOOKING_CANCELLED_BY_CUSTOMER, False),
        bc["perEventEnabled"],
        bc["emissionEnabled"],
        per.get(EVENT_DRIVER_ASSIGNMENT_CHANGED, False),
        da["perEventEnabled"],
        da["emissionEnabled"],
        per.get(EVENT_REQUEST_CREATED, False),
        rc["perEventEnabled"],
        rc["emissionEnabled"],
        per.get(EVENT_REQUEST_UPDATED, False),
        ru["perEventEnabled"],
        ru["emissionEnabled"],
        per.get(EVENT_REQUEST_CANCELLED, False),
        rx["perEventEnabled"],
        rx["emissionEnabled"],
        per.get(EVENT_REQUEST_REOPENED, False),
        rr["perEventEnabled"],
        rr["emissionEnabled"],
        payload["DOMAIN_EVENTS_ENABLED_source"],
        flag_snapshot_booleans()["DOMAIN_EVENT_BID_CREATED_ENABLED_source"],
        hc["source"],
        ha["source"],
        hr["source"],
        bc["source"],
        da["source"],
        rc["source"],
        ru["source"],
        rx["source"],
        rr["source"],
        any(enabled for et, enabled in per.items() if et != EVENT_BID_CREATED),
        per_pairs,
    )
    _logger.info(msg, *args)
    # Belt-and-suspenders: print mirrors logger so filesystem/logstream capture
    # works even if an upstream logging config clears handlers later.
    try:
        print(msg % args, flush=True)
    except Exception:
        pass


def log_handshake_cancelled_emission_decision(
    *,
    previous_status: str,
    transition_eligible: bool,
    append_attempted: bool,
    append_succeeded: bool,
) -> None:
    """
    B2 cancel-path decision marker (no RID / account identifiers).

    Emitted on the CONFIRMED→OPEN business path around maybe_append.
    """
    decision = describe_emission_decision(EVENT_HANDSHAKE_CANCELLED)
    msg = (
        "handshake_cancelled_emission_decision eventType=%s masterEnabled=%s "
        "perEventEnabled=%s previousStatus=%s transitionEligible=%s "
        "appendAttempted=%s appendSucceeded=%s deployRevision=%s "
        "instanceHash=%s emissionEnabled=%s"
    )
    # Sanitize status enum to a short token (no free text).
    status_token = str(previous_status or "").strip()[:40]
    args = (
        EVENT_HANDSHAKE_CANCELLED,
        decision["master_enabled"],
        decision["per_event_enabled"],
        status_token,
        transition_eligible,
        append_attempted,
        append_succeeded,
        decision["revision"],
        decision["instance_hash"],
        decision["emission_enabled"],
    )
    _logger.info(msg, *args)
    try:
        print(msg % args, flush=True)
    except Exception:
        pass


def describe_emission_decision(event_type: str) -> Dict[str, Any]:
    """Return sanitized emission decision details for diagnostics/tests."""
    master = domain_events_enabled()
    per_event = event_type_enabled(event_type)
    enabled = bool(master and per_event)
    flag_name = EVENT_TYPE_FLAG_ENV.get(event_type)
    return {
        "event_type": event_type,
        "emission_enabled": enabled,
        "master_enabled": bool(master),
        "per_event_enabled": bool(per_event),
        "per_event_flag": flag_name,
        "master_source": env_flag_source_category("DOMAIN_EVENTS_ENABLED"),
        "per_event_source": (
            env_flag_source_category(flag_name) if flag_name else CONFIG_SOURCE_DEFAULT
        ),
        "revision": deployment_revision_hint(),
        "instance_hash": instance_id_hash(),
    }


def log_emission_decision(event_type: str, *, decision: Optional[Dict[str, Any]] = None) -> None:
    """Log emission gate decision (boolean flags only)."""
    d = decision or describe_emission_decision(event_type)
    _logger.info(
        "domain_event_emission_decision eventType=%s enabled=%s "
        "master=%s per_event=%s master_source=%s per_event_source=%s "
        "revision=%s instance_hash=%s",
        d["event_type"],
        d["emission_enabled"],
        d["master_enabled"],
        d["per_event_enabled"],
        d["master_source"],
        d["per_event_source"],
        d["revision"],
        d["instance_hash"],
    )


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
    # INFO when master is on (rollback / per-event off) so ops can prove suppression.
    # DEBUG when master is off to avoid spam on every mutation with flags default-off.
    decision = describe_emission_decision(event_type)
    log_fn = _logger.info if decision["master_enabled"] else _logger.debug
    log_fn(
        "outbox_skipped_disabled eventType=%s metric_outbox_skipped_disabled=%s "
        "master=%s per_event=%s revision=%s instance_hash=%s",
        event_type,
        _METRIC_OUTBOX_SKIPPED_DISABLED[event_type],
        decision["master_enabled"],
        decision["per_event_enabled"],
        decision["revision"],
        decision["instance_hash"],
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
    decision = describe_emission_decision(event_type)
    if not decision["emission_enabled"]:
        record_event_skipped_disabled(event_type)
        return None
    # Log affirmative gate once per append path (pairs with outbox_event_appended).
    log_emission_decision(event_type, decision=decision)
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
        "flag_malformed": dict(_METRIC_FLAG_MALFORMED),
    }


def _reset_metrics_for_tests() -> None:
    """Test helper: clear in-process counters between cases."""
    global _METRIC_BID_CREATED_EMITTED, _METRIC_BID_CREATED_SKIPPED_DISABLED
    _METRIC_OUTBOX_APPENDED.clear()
    _METRIC_OUTBOX_SKIPPED_DISABLED.clear()
    _METRIC_OUTBOX_APPEND_FAILED.clear()
    _METRIC_FLAG_MALFORMED.clear()
    _METRIC_BID_CREATED_EMITTED = 0
    _METRIC_BID_CREATED_SKIPPED_DISABLED = 0
