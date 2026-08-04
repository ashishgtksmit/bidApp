#!/usr/bin/env python3
"""Audit PR38 authSubjectId column after migration.

Read-only. Exit 0 when invariants hold; exit 1 otherwise.
Does not claim production execution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _has_unique_index(conn, column: str) -> bool:
    from sqlalchemy import text

    rows = conn.execute(text("SHOW INDEX FROM usertable")).fetchall()
    for row in rows:
        try:
            non_unique = int(row[1])
            col_name = str(row[4])
        except Exception:
            continue
        if col_name == column and non_unique == 0:
            return True
    return False


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    database_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")
    if not database_url:
        print("ERROR: Set DATABASE_URL (or SQLALCHEMY_DATABASE_URL).")
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    ok = True

    with engine.connect() as conn:
        row = conn.execute(
            text("SHOW COLUMNS FROM usertable LIKE 'authSubjectId'")
        ).fetchone()
        if row is None:
            print("FAIL: missing column authSubjectId")
            ok = False
        else:
            # Nullability is field index 2 in SHOW COLUMNS
            nullable = str(row[2]).upper()
            print(f"column authSubjectId: {row}")
            if nullable == "YES":
                print("FAIL: authSubjectId is nullable")
                ok = False

        null_sid = conn.execute(
            text(
                "SELECT COUNT(*) FROM usertable "
                "WHERE authSubjectId IS NULL OR authSubjectId = ''"
            )
        ).scalar()
        dup = conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT authSubjectId FROM usertable "
                "  GROUP BY authSubjectId HAVING COUNT(*) > 1"
                ") t"
            )
        ).scalar()
        total = conn.execute(text("SELECT COUNT(*) FROM usertable")).scalar()
        print(
            f"rows={total} null_or_empty_authSubjectId={null_sid} "
            f"duplicate_authSubjectId_groups={dup}"
        )
        if null_sid or dup:
            ok = False

        if not _has_unique_index(conn, "authSubjectId"):
            print("FAIL: unique index on authSubjectId missing")
            ok = False
        else:
            print("OK: unique index on authSubjectId present")

    if not ok:
        print("Audit FAILED.")
        return 1
    print("Audit OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
