#!/usr/bin/env python3
"""Apply PR60 Tours day route: per-day cities and drop point on request_itinerary_days.

Default is a dry run (read-only). Pass --apply to execute. Each column is
added only when missing; MySQL DDL auto-commits.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MIG_DIR.parent / "pr50_sightseeing"))

from _db import DAYS_TABLE, column_exists, make_engine, table_exists  # noqa: E402

COLUMNS = {
    "fromLocation": f"ALTER TABLE {DAYS_TABLE} ADD COLUMN fromLocation VARCHAR(100) NULL",
    "toLocation": f"ALTER TABLE {DAYS_TABLE} ADD COLUMN toLocation VARCHAR(100) NULL",
    "dropLandmark": f"ALTER TABLE {DAYS_TABLE} ADD COLUMN dropLandmark VARCHAR(200) NULL",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    engine = make_engine()
    with engine.connect() as conn:
        if not table_exists(conn, DAYS_TABLE):
            print(f"ERROR: {DAYS_TABLE} is missing (apply PR50 first).")
            return 1
        missing = [name for name in COLUMNS if not column_exists(conn, DAYS_TABLE, name)]
        print("Planned changes:")
        for name in COLUMNS:
            print(f"  {DAYS_TABLE}.{name}: {'ADD COLUMN' if name in missing else 'exists'}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to execute.")
            return 0

        for name in missing:
            conn.execute(text(COLUMNS[name]))
            print(f"Added {DAYS_TABLE}.{name}.")
        conn.commit()

        result = ", ".join(
            f"{name}={'ok' if column_exists(conn, DAYS_TABLE, name) else 'MISSING'}" for name in COLUMNS
        )
        print(f"Verify: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
