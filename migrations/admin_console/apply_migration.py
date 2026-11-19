#!/usr/bin/env python3
"""Apply Admin Console tables: admin_users, admin_audit_log.

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

SCHEMA_SQL = MIG_DIR / "001_admin_tables.sql"
TABLES = ("admin_users", "admin_audit_log")


def _statements() -> dict[str, str]:
    body = "\n".join(
        line for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("--")
    )
    out: dict[str, str] = {}
    for stmt in body.split(";"):
        stmt = stmt.strip()
        m = re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", stmt, re.I)
        if m:
            out[m.group(1)] = stmt
    missing = [t for t in TABLES if t not in out]
    if missing:
        raise SystemExit(f"ERROR: CREATE TABLE not found for {missing}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    stmts = _statements()
    engine = make_engine()
    with engine.connect() as conn:
        existing = {t: table_exists(conn, t) for t in TABLES}
        print("Planned changes:")
        for t in TABLES:
            print(f"  {t}: {'exists' if existing[t] else 'CREATE'}")
        if not args.apply:
            print("\nDry run only. Re-run with --apply to execute.")
            return 0
        for t in TABLES:
            if not existing[t]:
                conn.execute(text(stmts[t]))
                conn.commit()
                print(f"Created {t}.")
            else:
                print(f"{t} already present.")
        print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
