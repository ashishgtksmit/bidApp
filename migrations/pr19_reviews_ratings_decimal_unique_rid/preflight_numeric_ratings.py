#!/usr/bin/env python3
"""PR19 preflight: report malformed rating values that cannot convert to DECIMAL(2,1).

Valid values: 0.5 through 5.0 inclusive, half-step only.
Does NOT coerce or repair rows. Exits non-zero on conflicts.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _is_valid_half_rating(raw) -> bool:
    if raw is None:
        return False
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return False
    if value < Decimal("0.5") or value > Decimal("5.0"):
        return False
    doubled = value * 2
    return doubled == doubled.to_integral_value()


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
    conflicts = 0

    vendor_cols = (
        "driverBehaviour",
        "punctuality",
        "carCondition",
        "cleanliness",
    )

    with engine.connect() as conn:
        print("=== Malformed vendorreviews category ratings ===")
        vendor_rows = conn.execute(
            text(
                """
                SELECT VRID, RID, driverBehaviour, punctuality, carCondition, cleanliness
                FROM vendorreviews
                ORDER BY VRID
                """
            )
        ).mappings().all()
        vendor_bad = []
        for row in vendor_rows:
            bad_cols = [
                col for col in vendor_cols if not _is_valid_half_rating(row[col])
            ]
            if bad_cols:
                vendor_bad.append((row, bad_cols))
        if not vendor_bad:
            print(f"OK: scanned {len(vendor_rows)} vendorreviews rows")
        else:
            conflicts += len(vendor_bad)
            for row, bad_cols in vendor_bad:
                print(
                    f"CONFLICT VRID={row['VRID']} RID={row['RID']} "
                    f"bad_columns={bad_cols} values="
                    f"({row['driverBehaviour']},{row['punctuality']},"
                    f"{row['carCondition']},{row['cleanliness']})"
                )

        print("=== Malformed customerreviews.generalRating ===")
        customer_rows = conn.execute(
            text(
                """
                SELECT CR, RID, generalRating
                FROM customerreviews
                ORDER BY CR
                """
            )
        ).mappings().all()
        customer_bad = []
        for row in customer_rows:
            if not _is_valid_half_rating(row["generalRating"]):
                customer_bad.append(row)
        if not customer_bad:
            print(f"OK: scanned {len(customer_rows)} customerreviews rows")
        else:
            conflicts += len(customer_bad)
            for row in customer_bad:
                print(
                    f"CONFLICT CR={row['CR']} RID={row['RID']} "
                    f"generalRating={row['generalRating']!r}"
                )

    if conflicts:
        print(
            f"FAIL: {conflicts} malformed rating row(s). "
            "Do not convert columns until repaired manually."
        )
        return 1

    print("PASS: all rating values are convertible to DECIMAL(2,1) half-steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
