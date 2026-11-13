#!/usr/bin/env python3
"""PR50 audit: read-only row counts and integrity checks for sightseeing tables."""

from __future__ import annotations

import sys

from _db import DAY_SPOTS_TABLE, DAYS_TABLE, SPOTS_TABLE, column_exists, make_engine, table_exists


def main() -> int:
    from sqlalchemy import text

    engine = make_engine()
    ok = True
    with engine.connect() as conn:
        for table in (SPOTS_TABLE, DAYS_TABLE, DAY_SPOTS_TABLE):
            if not table_exists(conn, table):
                print(f"MISSING table {table}")
                ok = False
        if not column_exists(conn, "requesttable", "tripEndDate"):
            print("MISSING column requesttable.tripEndDate")
            ok = False
        if not ok:
            return 1

        print("Spots per location:")
        for lid, name, total, active, permit in conn.execute(
            text(
                f"SELECT s.locationId, l.location, COUNT(*), SUM(s.isActive), SUM(s.permitRequired) "
                f"FROM {SPOTS_TABLE} s LEFT JOIN location_details l ON l.LID = s.locationId "
                f"GROUP BY s.locationId, l.location ORDER BY l.location"
            )
        ):
            print(f"  {name or 'UNKNOWN LID'} ({lid}): {total} spots, {active} active, {permit} permit")
            if name is None:
                ok = False

        orphan_days = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {DAYS_TABLE} d "
                f"LEFT JOIN requesttable r ON r.RID = d.requestId WHERE r.RID IS NULL"
            )
        ).scalar()
        empty_days = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {DAYS_TABLE} d "
                f"WHERE NOT EXISTS (SELECT 1 FROM {DAY_SPOTS_TABLE} x WHERE x.dayId = d.dayId)"
            )
        ).scalar()
        print(f"Itinerary days: orphaned={orphan_days} without spots={empty_days}")
        if orphan_days or empty_days:
            ok = False

    print("Audit OK." if ok else "Audit found issues.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
