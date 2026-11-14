#!/usr/bin/env python3
"""Apply PR64 booking integrity: user_devices table.

Default is a dry run (read-only). Pass --apply to execute. Idempotent.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MIG_DIR.parent / "pr50_sightseeing"))

from _db import make_engine, table_exists  # noqa: E402

SCHEMA_SQL = MIG_DIR / "schema_user_devices.sql"
TABLE = "user_devices"


def _create_statement() -> str:
    body = "\n".join(
        line
        for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    for stmt in body.split(";"):
        stmt = stmt.strip()
        if re.search(r"CREATE TABLE IF NOT EXISTS\s+user_devices", stmt, re.I):
            return stmt
    raise SystemExit("ERROR: CREATE TABLE user_devices not found in schema SQL.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    engine = make_engine()
    create_sql = _create_statement()
    with engine.connect() as conn:
        exists = table_exists(conn, TABLE)
        print("Planned changes:")
        print(f"  {TABLE}: {'exists' if exists else 'CREATE'}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to execute.")
            return 0

        if not exists:
            conn.execute(text(create_sql))
            conn.commit()
            print(f"Created {TABLE}.")
        else:
            print(f"{TABLE} already present.")

        print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
