#!/usr/bin/env python3
"""Apply PR38 authSubjectId migration.

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

from preflight_immutable_auth_subject import main as preflight_main  # noqa: E402


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
        if _column_meta(conn, "authSubjectId") is None:
            conn.execute(
                text(
                    "ALTER TABLE usertable "
                    "ADD COLUMN authSubjectId VARCHAR(64) NULL"
                )
            )
            print("Added nullable authSubjectId.")
        else:
            print("authSubjectId already present.")

        # Backfill unique authSubjectId for every row (including tombstones).
        # Never derive from phone / UID / email / accountSessionId.
        rows = conn.execute(
            text(
                "SELECT UID FROM usertable "
                "WHERE authSubjectId IS NULL OR authSubjectId = ''"
            )
        ).fetchall()
        for (uid,) in rows:
            new_id = uuid.uuid4().hex
            # Collision-safe retry
            for _ in range(5):
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM usertable "
                        "WHERE authSubjectId = :sid LIMIT 1"
                    ),
                    {"sid": new_id},
                ).fetchone()
                if exists is None:
                    break
                new_id = uuid.uuid4().hex
            conn.execute(
                text(
                    "UPDATE usertable SET authSubjectId = :sid "
                    "WHERE UID = :uid"
                ),
                {"sid": new_id, "uid": uid},
            )
        print(f"Backfilled authSubjectId for {len(rows)} row(s).")

        # Verify
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
        if null_sid or dup:
            print(
                f"ERROR: verify failed null_or_empty={null_sid} "
                f"dup_groups={dup}"
            )
            raise RuntimeError("backfill verification failed")

        # NOT NULL
        conn.execute(
            text(
                "ALTER TABLE usertable "
                "MODIFY COLUMN authSubjectId VARCHAR(64) NOT NULL"
            )
        )
        print("Column set NOT NULL.")

        if not _has_unique_index(conn, "authSubjectId"):
            conn.execute(
                text(
                    "ALTER TABLE usertable "
                    "ADD UNIQUE INDEX uq_usertable_auth_subject_id "
                    "(authSubjectId)"
                )
            )
            print("Added unique index on authSubjectId.")
        else:
            print("Unique index on authSubjectId already present.")

    print("Migration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
