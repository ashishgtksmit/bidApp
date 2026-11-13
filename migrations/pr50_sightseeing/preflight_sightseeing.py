#!/usr/bin/env python3
"""PR50 preflight: read-only checks before creating the sightseeing schema.

Reports MySQL version, request types, location mapping, requesttable key type,
existing PR50 objects and their compatibility. Never writes.
"""

from __future__ import annotations

import sys

from _db import (
    DAY_SPOTS_TABLE,
    DAYS_TABLE,
    SIGHTSEEING_REQUEST_TYPE_NAME,
    SPOTS_TABLE,
    column_exists,
    make_engine,
    map_locations,
    table_exists,
)


def main() -> int:
    from sqlalchemy import text

    engine = make_engine()
    ok = True
    with engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar()
        print(f"MySQL version: {version}")
        if not str(version).startswith(("8.", "9.")):
            print("ERROR: MySQL 8+ required.")
            ok = False

        for table in ("requesttable", "location_details", "requesttypedetails", "usertable"):
            if not table_exists(conn, table):
                print(f"ERROR: required table {table} missing.")
                ok = False

        rid_type = conn.execute(
            text(
                "SELECT COLUMN_TYPE FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'requesttable' AND column_name = 'RID'"
            )
        ).scalar()
        lid_type = conn.execute(
            text(
                "SELECT COLUMN_TYPE FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'location_details' AND column_name = 'LID'"
            )
        ).scalar()
        print(f"requesttable.RID type: {rid_type}; location_details.LID type: {lid_type}")

        print("requesttypedetails rows:")
        sightseeing_rows = 0
        for rtdid, name in conn.execute(
            text("SELECT RTDID, requestType FROM requesttypedetails ORDER BY RTDID")
        ).fetchall():
            print(f"  {rtdid}: {name}")
            if str(name).strip().lower() == SIGHTSEEING_REQUEST_TYPE_NAME.lower():
                sightseeing_rows += 1
        if sightseeing_rows > 1:
            print("ERROR: more than one Sightseeing request type row.")
            ok = False

        print("Location mapping (WizzRide code -> location_details.LID):")
        for code, lid in map_locations(conn).items():
            print(f"  {code}: {lid if lid is not None else 'UNMATCHED'}")

        print(
            "requesttable.tripEndDate present: "
            f"{column_exists(conn, 'requesttable', 'tripEndDate')}"
        )
        for table in (SPOTS_TABLE, DAYS_TABLE, DAY_SPOTS_TABLE):
            present = table_exists(conn, table)
            count = (
                conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() if present else 0
            )
            print(f"{table} present: {present} rows: {count}")

        grants = conn.execute(text("SHOW GRANTS")).fetchall()
        grant_text = " ".join(str(g[0]) for g in grants).upper()
        if "ALL PRIVILEGES" not in grant_text and not all(
            p in grant_text for p in ("CREATE", "ALTER", "INSERT", "REFERENCES")
        ):
            print("WARNING: could not confirm CREATE/ALTER/INSERT/REFERENCES privileges.")

    print("Preflight OK." if ok else "Preflight FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
