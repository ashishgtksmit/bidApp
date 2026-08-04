#!/usr/bin/env python3
"""Audit PR37 session identity columns after migration.

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
        for col in ("sessionVersion", "accountSessionId"):
            row = conn.execute(
                text(f"SHOW COLUMNS FROM usertable LIKE '{col}'")
            ).fetchone()
            if row is None:
                print(f"FAIL: missing column {col}")
                ok = False
            else:
                # Nullability is field index 2 in SHOW COLUMNS
                nullable = str(row[2]).upper()
                print(f"column {col}: {row}")
                if nullable == "YES":
                    print(f"FAIL: {col} is nullable")
                    ok = False

        null_sv = conn.execute(
            text(
                "SELECT COUNT(*) FROM usertable WHERE sessionVersion IS NULL"
            )
        ).scalar()
        null_sid = conn.execute(
            text(
                "SELECT COUNT(*) FROM usertable "
                "WHERE accountSessionId IS NULL OR accountSessionId = ''"
            )
        ).scalar()
        dup = conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT accountSessionId FROM usertable "
                "  GROUP BY accountSessionId HAVING COUNT(*) > 1"
                ") t"
            )
        ).scalar()
        total = conn.execute(text("SELECT COUNT(*) FROM usertable")).scalar()
        print(
            f"rows={total} null_sessionVersion={null_sv} "
            f"null_or_empty_accountSessionId={null_sid} "
            f"duplicate_accountSessionId_groups={dup}"
        )
        if null_sv or null_sid or dup:
            ok = False

    if not ok:
        print("Audit FAILED.")
        return 1
    print("Audit OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
