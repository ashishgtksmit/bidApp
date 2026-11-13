#!/usr/bin/env python3
"""PR60 Tours launch: rename the Tour request type to exactly ``Tours``.

The app shows the Tours option and FastAPI accepts tours only while a
``requesttypedetails`` row is named exactly ``Tours``, so this rename is the
feature switch. Production RTDID 4 is ``Tour`` (no requests). Vendor
preferences are not changed: vendors that already have the id keep it.

Default is a dry run (read-only). Pass --apply to execute.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MIG_DIR.parent / "pr50_sightseeing"))

from _db import DAYS_TABLE, column_exists, make_engine  # noqa: E402

TOURS_REQUEST_TYPE_NAME = "Tours"
LEGACY_NAMES = {"tour", "tours"}


def _find_type_row(conn) -> tuple[int, str] | None:
    from sqlalchemy import text

    rows = [
        (int(rid), str(name))
        for rid, name in conn.execute(text("SELECT RTDID, requestType FROM requesttypedetails"))
        if "".join(str(name or "").split()).lower() in LEGACY_NAMES
    ]
    if len(rows) > 1:
        raise SystemExit("ERROR: multiple Tour rows in requesttypedetails; fix manually.")
    return rows[0] if rows else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    engine = make_engine()
    with engine.connect() as conn:
        if not column_exists(conn, DAYS_TABLE, "toLocation"):
            print("ERROR: PR60 schema not applied (run apply_migration.py --apply first).")
            return 1
        row = _find_type_row(conn)
        if row is None:
            print("ERROR: no Tour request type row found.")
            return 1
        requests = conn.execute(
            text("SELECT COUNT(*) FROM requesttable WHERE requestType = :id"), {"id": row[0]}
        ).scalar()
        prefs = [
            p or ""
            for (p,) in conn.execute(
                text("SELECT requestTypePreferences FROM usertable WHERE alsoVendor = 1")
            )
        ]
        opted = sum(1 for p in prefs if str(row[0]) in [x.strip() for x in p.split(",")])

    print("Planned launch changes:")
    if row[1] == TOURS_REQUEST_TYPE_NAME:
        print(f"  requesttypedetails: already '{TOURS_REQUEST_TYPE_NAME}' (RTDID {row[0]})")
    else:
        print(f"  requesttypedetails: RENAME RTDID {row[0]} '{row[1]}' -> '{TOURS_REQUEST_TYPE_NAME}'")
    print(f"  existing requests with this type: {requests}")
    print(f"  vendors already opted in: {opted} of {len(prefs)} (unchanged)")

    if not args.apply or row[1] == TOURS_REQUEST_TYPE_NAME:
        if not args.apply:
            print("\nDry run only. Re-run with --apply to execute.")
        return 0

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE requesttypedetails SET requestType = :n WHERE RTDID = :id"),
            {"n": TOURS_REQUEST_TYPE_NAME, "id": row[0]},
        )
    print(f"Renamed RTDID {row[0]} '{row[1]}' -> '{TOURS_REQUEST_TYPE_NAME}'.")
    print("Apps pick it up on the next cold start (request types are cached per process).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
