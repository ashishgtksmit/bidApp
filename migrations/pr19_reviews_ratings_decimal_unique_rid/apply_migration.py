#!/usr/bin/env python3
"""Apply PR19 review rating decimal conversion + unique RID indexes.

Runs duplicate + numeric preflights first. Stops on conflicts.
Does NOT delete duplicate rows or coerce malformed ratings.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(MIG_DIR) not in sys.path:
    sys.path.insert(0, str(MIG_DIR))

from preflight_duplicate_review_rids import main as duplicate_preflight  # noqa: E402
from preflight_numeric_ratings import main as numeric_preflight  # noqa: E402


def _index_names(conn, table: str) -> set[str]:
    from sqlalchemy import text

    return {row[2] for row in conn.execute(text(f"SHOW INDEX FROM {table}")).fetchall()}


def _column_type(conn, table: str, column: str) -> str:
    from sqlalchemy import text

    row = conn.execute(
        text(
            """
            SELECT COLUMN_TYPE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
              AND COLUMN_NAME = :column
            """
        ),
        {"table": table, "column": column},
    ).fetchone()
    return (row[0] if row else "") or ""


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

    print("Running duplicate RID preflight…")
    if duplicate_preflight() != 0:
        return 1
    print("Running numeric rating preflight…")
    if numeric_preflight() != 0:
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    with engine.begin() as conn:
        vendor_cols = (
            "driverBehaviour",
            "punctuality",
            "carCondition",
            "cleanliness",
        )
        for col in vendor_cols:
            col_type = _column_type(conn, "vendorreviews", col).lower()
            if "decimal(2,1)" not in col_type:
                print(f"ALTER vendorreviews.{col} → DECIMAL(2,1)")
                conn.execute(
                    text(
                        f"ALTER TABLE vendorreviews "
                        f"MODIFY COLUMN {col} DECIMAL(2,1) NOT NULL"
                    )
                )
            else:
                print(f"OK vendorreviews.{col} already DECIMAL(2,1)")

        cust_type = _column_type(conn, "customerreviews", "generalRating").lower()
        if "decimal(2,1)" not in cust_type:
            print("ALTER customerreviews.generalRating → DECIMAL(2,1)")
            conn.execute(
                text(
                    "ALTER TABLE customerreviews "
                    "MODIFY COLUMN generalRating DECIMAL(2,1) NOT NULL"
                )
            )
        else:
            print("OK customerreviews.generalRating already DECIMAL(2,1)")

        vendor_indexes = _index_names(conn, "vendorreviews")
        if "uq_vendorreviews_rid" not in vendor_indexes:
            print("CREATE UNIQUE INDEX uq_vendorreviews_rid")
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_vendorreviews_rid "
                    "ON vendorreviews (RID)"
                )
            )
        else:
            print("OK uq_vendorreviews_rid exists")

        customer_indexes = _index_names(conn, "customerreviews")
        if "uq_customerreviews_rid" not in customer_indexes:
            print("CREATE UNIQUE INDEX uq_customerreviews_rid")
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_customerreviews_rid "
                    "ON customerreviews (RID)"
                )
            )
        else:
            print("OK uq_customerreviews_rid exists")

    print("PASS: PR19 migration applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
