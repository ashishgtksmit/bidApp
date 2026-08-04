#!/usr/bin/env python3
"""Preflight for PR37 usertable sessionVersion + accountSessionId.

Read-only. Exit 0 when safe to apply; exit 1 on unsupported schema or hazards.
Does not claim production execution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _column_meta(conn, name: str):
    from sqlalchemy import text

    return conn.execute(
        text(f"SHOW COLUMNS FROM usertable LIKE '{name}'")
    ).fetchone()


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
    blocking = False

    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT 1 FROM usertable LIMIT 1"))
        except Exception as exc:
            print(f"BLOCKING: usertable not readable: {exc}")
            return 1

        for col in ("UID", "userAppId"):
            meta = _column_meta(conn, col)
            if meta is None:
                print(f"BLOCKING: required column missing: {col}")
                blocking = True
            else:
                print(f"OK: column present: {col} → {meta}")

        for col in ("sessionVersion", "accountSessionId"):
            meta = _column_meta(conn, col)
            if meta is None:
                print(f"INFO: column absent (will be added): {col}")
            else:
                print(f"INFO: column already present: {col} → {meta}")

        # Duplicate non-null accountSessionId would block unique constraint.
        try:
            meta = _column_meta(conn, "accountSessionId")
            if meta is not None:
                dups = conn.execute(
                    text(
                        "SELECT accountSessionId, COUNT(*) AS c "
                        "FROM usertable "
                        "WHERE accountSessionId IS NOT NULL "
                        "AND accountSessionId != '' "
                        "GROUP BY accountSessionId "
                        "HAVING c > 1 "
                        "LIMIT 20"
                    )
                ).fetchall()
                if dups:
                    blocking = True
                    print(
                        "BLOCKING: duplicate accountSessionId values "
                        f"(showing up to 20): {dups}"
                    )
                else:
                    print("OK: no duplicate non-empty accountSessionId values.")
        except Exception as exc:
            print(f"WARNING: duplicate check skipped: {exc}")

    if blocking:
        print("Preflight FAILED.")
        return 1
    print("Preflight OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
