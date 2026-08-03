#!/usr/bin/env python3
"""Preflight for PR24 usertable.userAppId expansion to VARCHAR(64).

Reports:
* current column type / length when available
* existing userAppId values longer than 64 (blocking)
* existing values that already look like tombstones longer than legacy 10

Does not mutate the database.
Exit 0 when safe to apply; exit 1 on blocking issues.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET_LEN = 64


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
            col = conn.execute(
                text(
                    "SHOW COLUMNS FROM usertable LIKE 'userAppId'"
                )
            ).fetchone()
            if col is not None:
                print(f"Current userAppId column: {col}")
            else:
                print("WARNING: Could not read userAppId column metadata.")
        except Exception as exc:
            print(f"WARNING: SHOW COLUMNS failed: {exc}")

        try:
            over = conn.execute(
                text(
                    "SELECT userAppId, CHAR_LENGTH(userAppId) AS len "
                    "FROM usertable "
                    "WHERE CHAR_LENGTH(userAppId) > :maxlen "
                    "LIMIT 50"
                ),
                {"maxlen": TARGET_LEN},
            ).fetchall()
            if over:
                blocking = True
                print(
                    f"BLOCKING: {len(over)} userAppId value(s) exceed "
                    f"{TARGET_LEN} characters (showing up to 50):"
                )
                for row in over:
                    print(f"  len={row[1]} id={row[0]!r}")
            else:
                print(
                    f"OK: no existing userAppId values exceed {TARGET_LEN}."
                )
        except Exception as exc:
            blocking = True
            print(f"ERROR: length preflight query failed: {exc}")

        try:
            longish = conn.execute(
                text(
                    "SELECT COUNT(*) FROM usertable "
                    "WHERE CHAR_LENGTH(userAppId) > 10"
                )
            ).scalar()
            print(
                f"INFO: {longish} row(s) already have userAppId length > 10 "
                "(expected for prior tombstones if column was already wider)."
            )
        except Exception as exc:
            print(f"WARNING: >10 length count failed: {exc}")

    if blocking:
        print("Preflight FAILED — resolve blockers before migration.")
        return 1
    print("Preflight OK — safe to apply VARCHAR(64) expansion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
