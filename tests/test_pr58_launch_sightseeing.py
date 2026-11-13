"""PR58 launch script: vendor preference backfill rules (no DB)."""

import importlib.util
import sys
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parents[1] / "migrations" / "pr50_sightseeing"


def _load():
    sys.path.insert(0, str(MIG_DIR))
    try:
        spec = importlib.util.spec_from_file_location("launch_sightseeing", MIG_DIR / "launch_sightseeing.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(MIG_DIR))


launch = _load()


def test_appends_sightseeing_id():
    assert launch.next_preferences("1,2,3,4", 7) == "1,2,3,4,7"


def test_already_opted_in_is_unchanged():
    assert launch.next_preferences("1, 7 ,3", 7) is None


def test_empty_preferences_get_onboarding_default():
    assert launch.next_preferences(None, 7) == "1,2,3,4,7"
    assert launch.next_preferences(" , ", 7) == "1,2,3,4,7"


def test_legacy_sight_seeing_name_matches():
    assert launch._normalize("Sight Seeing") == launch._normalize("Sightseeing")
    assert launch._normalize("Tour") != launch._normalize("Sightseeing")


def test_does_not_match_id_substrings():
    assert launch.next_preferences("17,27", 7) == "17,27,7"
