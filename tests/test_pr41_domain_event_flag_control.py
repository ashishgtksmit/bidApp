"""
Centralized domain-event feature-flag parser contract (PR41 flag-control fix).

Covers master + per-event AND gate, fail-closed malformed values, dotenv
non-override, and test isolation. Does not claim production enablement.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")

from app_v1.events.outbox import (  # noqa: E402
    CONFIG_SOURCE_DEFAULT,
    CONFIG_SOURCE_PROCESS_ENV,
    _reset_metrics_for_tests,
    bid_created_events_enabled,
    domain_events_enabled,
    env_flag_enabled,
    env_flag_source_category,
    event_emission_enabled,
    event_type_enabled,
    get_metrics,
)
from app_v1.events.registry import (  # noqa: E402
    EVENT_BID_CREATED,
    EVENT_TYPE_FLAG_ENV,
)

PR40_FLAGS = [
    "DOMAIN_EVENT_BID_UPDATED_ENABLED",
    "DOMAIN_EVENT_BID_DELETED_ENABLED",
    "DOMAIN_EVENT_BID_ACCEPTED_ENABLED",
    "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED",
    "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED",
    "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED",
    "DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED",
    "DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED",
]


@pytest.fixture(autouse=True)
def _isolate_flags(monkeypatch):
    """No cached flag state; clear metrics; strip domain flags between tests."""
    _reset_metrics_for_tests()
    monkeypatch.delenv("DOMAIN_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv("DOMAIN_EVENT_BID_CREATED_ENABLED", raising=False)
    for name in PR40_FLAGS:
        monkeypatch.delenv(name, raising=False)
    yield
    _reset_metrics_for_tests()


def test_01_master_absent_fail_closed():
    assert env_flag_enabled("DOMAIN_EVENTS_ENABLED") is False
    assert domain_events_enabled() is False
    assert env_flag_source_category("DOMAIN_EVENTS_ENABLED") == CONFIG_SOURCE_DEFAULT


def test_02_master_false(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    assert domain_events_enabled() is False


def test_03_master_true(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    assert domain_events_enabled() is True
    assert env_flag_source_category("DOMAIN_EVENTS_ENABLED") == CONFIG_SOURCE_PROCESS_ENV


def test_04_per_event_absent_fail_closed():
    assert bid_created_events_enabled() is False
    assert event_type_enabled(EVENT_BID_CREATED) is False


def test_05_per_event_false(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    assert bid_created_events_enabled() is False


def test_06_per_event_true(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "true")
    assert bid_created_events_enabled() is True


def test_07_mixed_case_true(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "TRUE")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "Yes")
    assert domain_events_enabled() is True
    assert bid_created_events_enabled() is True
    assert event_emission_enabled(EVENT_BID_CREATED) is True


def test_08_whitespace_around_false(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "  false  ")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "\tOFF\n")
    assert domain_events_enabled() is False
    assert bid_created_events_enabled() is False


def test_09_malformed_fail_closed_with_metric(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "maybe")
    assert env_flag_enabled("DOMAIN_EVENTS_ENABLED") is False
    assert get_metrics()["flag_malformed"].get("DOMAIN_EVENTS_ENABLED", 0) >= 1


def test_10_master_true_event_false(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    assert event_emission_enabled(EVENT_BID_CREATED) is False


def test_11_master_false_event_true(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "false")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "true")
    assert event_emission_enabled(EVENT_BID_CREATED) is False


def test_12_both_true(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "true")
    assert event_emission_enabled(EVENT_BID_CREATED) is True


def test_13_all_pr40_flags_default_false():
    for event_type, flag in EVENT_TYPE_FLAG_ENV.items():
        if event_type == EVENT_BID_CREATED:
            continue
        assert flag in PR40_FLAGS
        assert event_type_enabled(event_type) is False
        assert event_emission_enabled(event_type) is False


def test_14_bid_created_defaults_false():
    assert bid_created_events_enabled() is False
    assert event_emission_enabled(EVENT_BID_CREATED) is False


def test_15_dotenv_cannot_override_process_env(tmp_path, monkeypatch):
    """load_dotenv(override=False) must not clobber an already-set process env."""
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("DOMAIN_EVENT_BID_CREATED_ENABLED=true\n", encoding="utf-8")
    from dotenv import load_dotenv

    loaded = load_dotenv(dotenv_file, override=False)
    assert loaded is True
    assert os.environ["DOMAIN_EVENT_BID_CREATED_ENABLED"] == "false"
    assert bid_created_events_enabled() is False


def test_16_monkeypatch_override_isolation(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "true")
    assert event_emission_enabled(EVENT_BID_CREATED) is True
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "false")
    assert event_emission_enabled(EVENT_BID_CREATED) is False


def test_17_no_cached_value_leaks_between_calls(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "true")
    assert event_emission_enabled(EVENT_BID_CREATED) is True
    monkeypatch.delenv("DOMAIN_EVENT_BID_CREATED_ENABLED", raising=False)
    assert event_emission_enabled(EVENT_BID_CREATED) is False
    monkeypatch.setenv("DOMAIN_EVENT_BID_CREATED_ENABLED", "1")
    assert event_emission_enabled(EVENT_BID_CREATED) is True


def test_master_does_not_imply_all_events(monkeypatch):
    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    # all per-event unset
    assert event_emission_enabled(EVENT_BID_CREATED) is False
    for event_type in EVENT_TYPE_FLAG_ENV:
        assert event_emission_enabled(event_type) is False


def test_process_bound_flag_snapshot_includes_handshake_cancelled(monkeypatch):
    """B2 binding proof payload must expose handshake.cancelled gate explicitly."""
    from app_v1.events.outbox import process_bound_flag_snapshot
    from app_v1.events.registry import EVENT_HANDSHAKE_CANCELLED

    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED", "true")
    monkeypatch.setenv("OPENBID_DEPLOY_REVISION", "testrev1")
    snap = process_bound_flag_snapshot(reason="unit")
    assert snap["DOMAIN_EVENTS_ENABLED"] is True
    assert snap["perEvent"][EVENT_HANDSHAKE_CANCELLED] is True
    assert snap["handshakeCancelled"]["eventType"] == "handshake.cancelled"
    assert snap["handshakeCancelled"]["envFlag"] == (
        "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED"
    )
    assert snap["handshakeCancelled"]["perEventEnabled"] is True
    assert snap["handshakeCancelled"]["emissionEnabled"] is True
    assert snap["deployRevision"] == "testrev1"
    assert "instanceHash" in snap
    # No PII keys
    blob = str(snap).lower()
    assert "rid" not in blob
    assert "phone" not in blob
    assert "password" not in blob


def test_process_bound_flag_snapshot_includes_handshake_accepted(monkeypatch):
    """B3 binding proof payload must expose handshake.accepted gate explicitly."""
    from app_v1.events.outbox import process_bound_flag_snapshot
    from app_v1.events.registry import EVENT_HANDSHAKE_ACCEPTED

    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED", "true")
    monkeypatch.setenv("OPENBID_DEPLOY_REVISION", "testrev-b3")
    snap = process_bound_flag_snapshot(reason="unit")
    assert snap["DOMAIN_EVENTS_ENABLED"] is True
    assert snap["perEvent"][EVENT_HANDSHAKE_ACCEPTED] is True
    assert snap["handshakeAccepted"]["eventType"] == "handshake.accepted"
    assert snap["handshakeAccepted"]["envFlag"] == (
        "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED"
    )
    assert snap["handshakeAccepted"]["perEventEnabled"] is True
    assert snap["handshakeAccepted"]["emissionEnabled"] is True
    assert snap["deployRevision"] == "testrev-b3"
    # Default / master-only: B3 emission remains false
    monkeypatch.delenv("DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED", raising=False)
    snap_off = process_bound_flag_snapshot(reason="unit")
    assert snap_off["handshakeAccepted"]["perEventEnabled"] is False
    assert snap_off["handshakeAccepted"]["emissionEnabled"] is False
    blob = str(snap).lower()
    assert "password" not in blob
    assert "fcm" not in blob


def test_handshake_cancelled_flag_env_mapping():
    from app_v1.events.registry import (
        EVENT_HANDSHAKE_CANCELLED,
        EVENT_TYPE_FLAG_ENV,
    )

    assert EVENT_HANDSHAKE_CANCELLED == "handshake.cancelled"
    assert (
        EVENT_TYPE_FLAG_ENV[EVENT_HANDSHAKE_CANCELLED]
        == "DOMAIN_EVENT_HANDSHAKE_CANCELLED_ENABLED"
    )


def test_handshake_accepted_flag_env_mapping():
    from app_v1.events.registry import (
        EVENT_HANDSHAKE_ACCEPTED,
        EVENT_TYPE_FLAG_ENV,
    )

    assert EVENT_HANDSHAKE_ACCEPTED == "handshake.accepted"
    assert (
        EVENT_TYPE_FLAG_ENV[EVENT_HANDSHAKE_ACCEPTED]
        == "DOMAIN_EVENT_HANDSHAKE_ACCEPTED_ENABLED"
    )


def test_process_bound_flag_snapshot_includes_handshake_rejected(monkeypatch):
    """B4 binding proof payload must expose handshake.rejected gate explicitly."""
    from app_v1.events.outbox import process_bound_flag_snapshot
    from app_v1.events.registry import EVENT_HANDSHAKE_REJECTED

    monkeypatch.setenv("DOMAIN_EVENTS_ENABLED", "true")
    monkeypatch.setenv("DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED", "true")
    monkeypatch.setenv("OPENBID_DEPLOY_REVISION", "testrev-b4")
    snap = process_bound_flag_snapshot(reason="unit")
    assert snap["DOMAIN_EVENTS_ENABLED"] is True
    assert snap["perEvent"][EVENT_HANDSHAKE_REJECTED] is True
    assert snap["handshakeRejected"]["eventType"] == "handshake.rejected"
    assert snap["handshakeRejected"]["envFlag"] == (
        "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED"
    )
    assert snap["handshakeRejected"]["perEventEnabled"] is True
    assert snap["handshakeRejected"]["emissionEnabled"] is True
    assert snap["deployRevision"] == "testrev-b4"
    monkeypatch.delenv("DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED", raising=False)
    snap_off = process_bound_flag_snapshot(reason="unit")
    assert snap_off["handshakeRejected"]["perEventEnabled"] is False
    assert snap_off["handshakeRejected"]["emissionEnabled"] is False
    blob = str(snap).lower()
    assert "password" not in blob
    assert "fcm" not in blob


def test_handshake_rejected_flag_env_mapping():
    from app_v1.events.registry import (
        EVENT_HANDSHAKE_REJECTED,
        EVENT_TYPE_FLAG_ENV,
    )

    assert EVENT_HANDSHAKE_REJECTED == "handshake.rejected"
    assert (
        EVENT_TYPE_FLAG_ENV[EVENT_HANDSHAKE_REJECTED]
        == "DOMAIN_EVENT_HANDSHAKE_REJECTED_ENABLED"
    )
