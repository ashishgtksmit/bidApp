#!/usr/bin/env python3
"""Apply PR37 sessionVersion + accountSessionId migration.

Runs preflight first. Staged nullable add → backfill → NOT NULL + unique index.
Idempotent where possible. Does not claim production applied.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(MIG_DIR) not in sys.path:
    sys.path.insert(0, str(MIG_DIR))

from preflight_account_session_identity import main as preflight_main  # noqa: E402


def _column_meta(conn, name: str):
    from sqlalchemy import text

    return conn.execute(
        text(f"SHOW COLUMNS FROM usertable LIKE '{name}'")
    ).fetchone()


def _has_unique_index(conn, column: str) -> bool:
    from sqlalchemy import text

    rows = conn.execute(text("SHOW INDEX FROM usertable")).fetchall()
    for row in rows:
        # Key_name, Non_unique, Column_name — positions vary; use names if available
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

    print("Running preflight…")
    if preflight_main() != 0:
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    with engine.begin() as conn:
        if _column_meta(conn, "sessionVersion") is None:
            conn.execute(
                text(
                    "ALTER TABLE usertable "
                    "ADD COLUMN sessionVersion INT NULL"
                )
            )
            print("Added nullable sessionVersion.")
        else:
            print("sessionVersion already present.")

        if _column_meta(conn, "accountSessionId") is None:
            conn.execute(
                text(
                    "ALTER TABLE usertable "
                    "ADD COLUMN accountSessionId VARCHAR(64) NULL"
                )
            )
            print("Added nullable accountSessionId.")
        else:
            print("accountSessionId already present.")

        # Backfill sessionVersion
        conn.execute(
            text(
                "UPDATE usertable SET sessionVersion = 1 "
                "WHERE sessionVersion IS NULL"
            )
        )

        # Backfill unique accountSessionId
        rows = conn.execute(
            text(
                "SELECT UID FROM usertable "
                "WHERE accountSessionId IS NULL OR accountSessionId = ''"
            )
        ).fetchall()
        for (uid,) in rows:
            new_id = uuid.uuid4().hex
            # Collision-safe retry
            for _ in range(5):
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM usertable "
                        "WHERE accountSessionId = :sid LIMIT 1"
                    ),
                    {"sid": new_id},
                ).fetchone()
                if exists is None:
                    break
                new_id = uuid.uuid4().hex
            conn.execute(
                text(
                    "UPDATE usertable SET accountSessionId = :sid "
                    "WHERE UID = :uid"
                ),
                {"sid": new_id, "uid": uid},
            )
        print(f"Backfilled accountSessionId for {len(rows)} row(s).")

        # Verify
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
        if null_sv or null_sid or dup:
            print(
                f"ERROR: verify failed null_sv={null_sv} "
                f"null_sid={null_sid} dup_groups={dup}"
            )
            raise RuntimeError("backfill verification failed")

        # NOT NULL
        conn.execute(
            text(
                "ALTER TABLE usertable "
                "MODIFY COLUMN sessionVersion INT NOT NULL"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE usertable "
                "MODIFY COLUMN accountSessionId VARCHAR(64) NOT NULL"
            )
        )
        print("Columns set NOT NULL.")

        if not _has_unique_index(conn, "accountSessionId"):
            conn.execute(
                text(
                    "ALTER TABLE usertable "
                    "ADD UNIQUE INDEX uq_usertable_account_session_id "
                    "(accountSessionId)"
                )
            )
            print("Added unique index on accountSessionId.")
        else:
            print("Unique index on accountSessionId already present.")

    print("Migration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
