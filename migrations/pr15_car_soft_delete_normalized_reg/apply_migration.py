#!/usr/bin/env python3
"""Apply PR15 cardetails soft-delete + normalizedCarRegNo unique index.

Runs preflight first. Stops on unresolved conflicts.
Uses SQLAlchemy connection from DATABASE_URL / SQLALCHEMY_DATABASE_URL.
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

from preflight_normalized_reg_conflicts import (  # noqa: E402
    main as preflight_main,
    normalize_car_registration,
)


def _column_names(conn) -> set[str]:
    from sqlalchemy import text

    return {row[0] for row in conn.execute(text("SHOW COLUMNS FROM cardetails")).fetchall()}


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
        cols = _column_names(conn)

        if "normalizedCarRegNo" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE cardetails "
                    "ADD COLUMN normalizedCarRegNo VARCHAR(100) NULL"
                )
            )
        if "isDeleted" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE cardetails "
                    "ADD COLUMN isDeleted TINYINT(1) NOT NULL DEFAULT 0"
                )
            )
        if "deletedAt" not in cols:
            conn.execute(
                text("ALTER TABLE cardetails ADD COLUMN deletedAt TIMESTAMP NULL")
            )
        if "deletedBy" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE cardetails "
                    "ADD COLUMN deletedBy VARCHAR(10) NULL"
                )
            )

        rows = conn.execute(
            text("SELECT CARID, carRegNo, normalizedCarRegNo FROM cardetails")
        ).mappings().all()

        for row in rows:
            existing = (row["normalizedCarRegNo"] or "").strip()
            if existing:
                continue
            normalized = normalize_car_registration(row["carRegNo"])
            if not normalized:
                print(
                    f"STOP: CARID={row['CARID']} has empty normalized registration "
                    f"from carRegNo={row['carRegNo']!r}"
                )
                return 1
            conn.execute(
                text(
                    "UPDATE cardetails SET normalizedCarRegNo = :n WHERE CARID = :id"
                ),
                {"n": normalized, "id": row["CARID"]},
            )

        conn.execute(
            text(
                "ALTER TABLE cardetails "
                "MODIFY COLUMN normalizedCarRegNo VARCHAR(100) NOT NULL"
            )
        )

        # Unique index — fail loudly if MySQL rejects
        indexes = {
            row[2]
            for row in conn.execute(text("SHOW INDEX FROM cardetails")).fetchall()
        }
        if "uq_cardetails_normalizedCarRegNo" not in indexes:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_cardetails_normalizedCarRegNo "
                    "ON cardetails (normalizedCarRegNo)"
                )
            )

    print("PR15 migration applied successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
