#!/usr/bin/env python3
"""PR58 sightseeing launch.

1. Makes ``requesttypedetails`` contain exactly ``Sightseeing`` (feature switch:
   the app shows the toggle and FastAPI accepts sightseeing only after this).
   An existing spacing/case variant (production RTDID 3 ``Sight Seeing``) is
   renamed; otherwise a row is inserted.
2. Backfills ``usertable.requestTypePreferences`` so every vendor
   (``alsoVendor = 1``) opts in to sightseeing pushes. Empty preferences get
   the onboarding default ``1,2,3,4`` plus the sightseeing id.

Default is a dry run (read-only). Pass --apply to execute in one transaction.
Changed vendor UIDs are printed (no names / phones / contact data).

Requires PR50 (apply_migration.py --apply) to have been applied first.
"""

from __future__ import annotations

import argparse
import sys

from _db import (
    DAYS_TABLE,
    SIGHTSEEING_REQUEST_TYPE_NAME,
    SPOTS_TABLE,
    column_exists,
    make_engine,
    table_exists,
)

DEFAULT_VENDOR_PREFS = ["1", "2", "3", "4"]


def _split_csv(value) -> list[str]:
    if value is None:
        return []
    return [p.strip() for p in str(value).split(",") if p.strip()]


def next_preferences(current, sightseeing_id: int) -> str | None:
    """New CSV for a vendor, or None when already opted in."""
    wanted = str(sightseeing_id)
    parts = _split_csv(current)
    if wanted in parts:
        return None
    if not parts:
        parts = list(DEFAULT_VENDOR_PREFS)
    return ",".join(parts + [wanted])


def _normalize(name) -> str:
    return "".join(str(name or "").split()).lower()


def _find_type_row(conn) -> tuple[int, str] | None:
    """Existing sightseeing type row, matching names that differ only in spacing/case
    (production has legacy RTDID 3 'Sight Seeing')."""
    from sqlalchemy import text

    rows = [
        (int(rid), str(name))
        for rid, name in conn.execute(text("SELECT RTDID, requestType FROM requesttypedetails"))
        if _normalize(name) == _normalize(SIGHTSEEING_REQUEST_TYPE_NAME)
    ]
    if len(rows) > 1:
        raise SystemExit("ERROR: multiple sightseeing rows in requesttypedetails; fix manually.")
    return rows[0] if rows else None


def _vendor_changes(conn, sightseeing_id: int | None):
    from sqlalchemy import text

    vendors = conn.execute(
        text("SELECT UID, requestTypePreferences FROM usertable WHERE alsoVendor = 1")
    ).fetchall()
    probe_id = sightseeing_id if sightseeing_id is not None else -1
    changes = []
    empty = 0
    for uid, prefs in vendors:
        if not _split_csv(prefs):
            empty += 1
        new = next_preferences(prefs, probe_id)
        if new is not None:
            changes.append((int(uid), new))
    return len(vendors), changes, empty


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    engine = make_engine()
    with engine.connect() as conn:
        missing = [t for t in (SPOTS_TABLE, DAYS_TABLE) if not table_exists(conn, t)]
        if missing or not column_exists(conn, "requesttable", "tripEndDate"):
            print("ERROR: PR50 schema not applied (run apply_migration.py --apply first).")
            return 1
        active_spots = conn.execute(
            text(f"SELECT COUNT(*) FROM {SPOTS_TABLE} WHERE isActive = 1")
        ).scalar()
        row = _find_type_row(conn)
        type_id = row[0] if row else None
        total, changes, empty = _vendor_changes(conn, type_id)

    print("Planned launch changes:")
    print(f"  active sightseeing spots: {active_spots}")
    if row is None:
        print(f"  requesttypedetails: INSERT '{SIGHTSEEING_REQUEST_TYPE_NAME}'")
    elif row[1] != SIGHTSEEING_REQUEST_TYPE_NAME:
        print(f"  requesttypedetails: RENAME RTDID {row[0]} '{row[1]}' -> '{SIGHTSEEING_REQUEST_TYPE_NAME}'")
    else:
        print(f"  requesttypedetails: exists (RTDID {row[0]})")
    print(f"  vendors (alsoVendor = 1): {total}")
    print(f"  vendors to opt in: {len(changes)} (of which empty preferences: {empty})")
    if not active_spots:
        print("WARNING: no active spots; customers would see an empty spot list.")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to execute.")
        return 0

    with engine.begin() as conn:
        row = _find_type_row(conn)
        if row is None:
            conn.execute(
                text("INSERT INTO requesttypedetails (requestType) VALUES (:n)"),
                {"n": SIGHTSEEING_REQUEST_TYPE_NAME},
            )
            row = _find_type_row(conn)
            print(f"Inserted Sightseeing request type (RTDID {row[0]}).")
        elif row[1] != SIGHTSEEING_REQUEST_TYPE_NAME:
            conn.execute(
                text("UPDATE requesttypedetails SET requestType = :n WHERE RTDID = :id"),
                {"n": SIGHTSEEING_REQUEST_TYPE_NAME, "id": row[0]},
            )
            print(f"Renamed RTDID {row[0]} '{row[1]}' -> '{SIGHTSEEING_REQUEST_TYPE_NAME}'.")
        type_id = row[0]
        _, changes, _ = _vendor_changes(conn, type_id)
        for uid, new in changes:
            conn.execute(
                text("UPDATE usertable SET requestTypePreferences = :p WHERE UID = :u"),
                {"p": new, "u": uid},
            )
        print(f"Opted in {len(changes)} vendors. UIDs: {[uid for uid, _ in changes]}")
    print("Launch applied. Apps pick it up on the next cold start (request types are cached per process).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
