#!/usr/bin/env python3
"""Apply PR24 usertable.userAppId VARCHAR(64) expansion.

Runs preflight first. Idempotent: skips ALTER when column is already >= 64.
Uses DATABASE_URL / SQLALCHEMY_DATABASE_URL. Does not claim production applied.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(MIG_DIR) not in sys.path:
    sys.path.insert(0, str(MIG_DIR))

from preflight_userappid_length import main as preflight_main  # noqa: E402

TARGET_LEN = 64


def _column_type(conn) -> str | None:
    from sqlalchemy import text

    row = conn.execute(
        text("SHOW COLUMNS FROM usertable LIKE 'userAppId'")
    ).fetchone()
    if row is None:
        return None
    # MySQL SHOW COLUMNS: Field, Type, Null, Key, Default, Extra
    return str(row[1] or "")


def _varchar_length(type_text: str) -> int | None:
    match = re.search(r"varchar\((\d+)\)", type_text, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


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

    print("Running preflight…")
    if preflight_main() != 0:
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    with engine.begin() as conn:
        type_text = _column_type(conn)
        if type_text is None:
            print("ERROR: Could not read userAppId column.")
            return 1
        current_len = _varchar_length(type_text)
        print(f"Detected userAppId type: {type_text}")
        if current_len is not None and current_len >= TARGET_LEN:
            print(
                f"Skip ALTER — column already VARCHAR({current_len}) >= {TARGET_LEN}."
            )
            return 0

        conn.execute(
            text(
                "ALTER TABLE usertable "
                f"MODIFY COLUMN userAppId VARCHAR({TARGET_LEN}) NOT NULL"
            )
        )
        print(f"Applied: userAppId → VARCHAR({TARGET_LEN}).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
