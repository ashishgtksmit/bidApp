#!/usr/bin/env python3
"""Apply PR50 sightseeing schema + spot seed.

Default is a dry run (read-only): prints what would be created and how many
spots each location would receive. Pass --apply to execute.

MySQL DDL auto-commits, so tables/column are created first (idempotent), then
the seed runs in a single transaction.
"""

from __future__ import annotations

import argparse
import re
import sys

from _db import (
    DAY_SPOTS_TABLE,
    DAYS_TABLE,
    MIG_DIR,
    SPOTS_TABLE,
    column_exists,
    make_engine,
    map_locations,
    table_exists,
)
from preflight_sightseeing import main as preflight_main

SCHEMA_SQL = MIG_DIR / "schema_sightseeing.sql"
SEED_SQL = MIG_DIR / "seed_sightseeing_spots.sql"

SPOT_CODE = re.compile(r"SELECT loc\.LID, '((?:[^']|'')+)'")
SPOT_LOCATION = re.compile(r"IN \('([^']+)'")


def _statements(path) -> list[str]:
    body = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    return [s.strip() for s in body.split(";\n") if s.strip().rstrip(";")]


def _seed_plan(conn) -> dict[str, int]:
    """Count seed statements per location name that would insert a new spot."""
    from sqlalchemy import text

    existing = set()
    if table_exists(conn, SPOTS_TABLE):
        existing = {r[0] for r in conn.execute(text(f"SELECT spotCode FROM {SPOTS_TABLE}"))}
    plan: dict[str, int] = {}
    for stmt in _statements(SEED_SQL):
        code = SPOT_CODE.search(stmt).group(1).replace("''", "'")
        location = SPOT_LOCATION.search(stmt).group(1)
        if code not in existing:
            plan[location] = plan.get(location, 0) + 1
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    if not SEED_SQL.exists():
        print("ERROR: run build_sightseeing_spot_seed.py first.")
        return 1

    print("Running preflight…")
    if preflight_main() != 0:
        return 1

    from sqlalchemy import text

    engine = make_engine()
    with engine.connect() as conn:
        mapping = map_locations(conn)
        plan = _seed_plan(conn)
        print("\nPlanned changes:")
        for table in (SPOTS_TABLE, DAYS_TABLE, DAY_SPOTS_TABLE):
            print(f"  {table}: {'exists' if table_exists(conn, table) else 'CREATE'}")
        print(
            "  requesttable.tripEndDate: "
            f"{'exists' if column_exists(conn, 'requesttable', 'tripEndDate') else 'ADD COLUMN'}"
        )
        print("  New spots per location (only matched locations insert rows):")
        lid_by_name = {}
        from _db import WIZZRIDE_LOCATION_NAMES

        for code, names in WIZZRIDE_LOCATION_NAMES.items():
            for name in names:
                lid_by_name[name.lower()] = mapping.get(code)
        for location, count in sorted(plan.items()):
            lid = lid_by_name.get(location)
            status = f"LID {lid}" if lid is not None else "UNMATCHED - skipped"
            print(f"    {location}: {count} ({status})")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to execute.")
        return 0

    with engine.connect() as conn:
        for stmt in _statements(SCHEMA_SQL):
            conn.execute(text(stmt))
        if not column_exists(conn, "requesttable", "tripEndDate"):
            conn.execute(text("ALTER TABLE requesttable ADD COLUMN tripEndDate DATE NULL"))
            print("Added requesttable.tripEndDate.")
        conn.commit()
        print("Schema ready.")

    inserted = 0
    with engine.begin() as conn:
        for stmt in _statements(SEED_SQL):
            inserted += conn.execute(text(stmt)).rowcount or 0
    print(f"Seed applied: {inserted} new spots.")
    print("Migration applied. Run audit_sightseeing.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
