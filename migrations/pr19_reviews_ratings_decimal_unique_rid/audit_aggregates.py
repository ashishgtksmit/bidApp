#!/usr/bin/env python3
"""PR19 aggregate audit — report only; never updates userTable.

Compares stored aggregates vs recalculated values from review rows.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TWO = Decimal("0.01")


def _round2(value: float) -> str:
    return str(Decimal(str(value)).quantize(_TWO, rounding=ROUND_HALF_UP))


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
    mismatches = 0

    with engine.connect() as conn:
        print("=== Vendor aggregate audit (userTable.rating / totalNoOfReviews) ===")
        vendors = conn.execute(
            text(
                """
                SELECT u.userAppId,
                       u.rating AS stored_rating,
                       u.totalNoOfReviews AS stored_count,
                       CAST(u.userAppId AS UNSIGNED) AS vendor_key
                FROM usertable u
                WHERE u.alsoVendor = 1
                ORDER BY u.userAppId
                """
            )
        ).mappings().all()

        for vendor in vendors:
            rows = conn.execute(
                text(
                    """
                    SELECT driverBehaviour, punctuality, carCondition, cleanliness
                    FROM vendorreviews
                    WHERE VENDORID = :vid
                    """
                ),
                {"vid": vendor["vendor_key"]},
            ).mappings().all()
            calc_count = len(rows)
            if calc_count == 0:
                calc_avg = "0.00"
            else:
                means = [
                    (
                        float(r["driverBehaviour"])
                        + float(r["punctuality"])
                        + float(r["carCondition"])
                        + float(r["cleanliness"])
                    )
                    / 4.0
                    for r in rows
                ]
                calc_avg = _round2(sum(means) / calc_count)

            stored_rating = _round2(float(vendor["stored_rating"] or 0))
            stored_count = int(vendor["stored_count"] or 0)
            if stored_rating != calc_avg or stored_count != calc_count:
                mismatches += 1
                print(
                    f"MISMATCH vendor={vendor['userAppId']} "
                    f"stored=({stored_rating},{stored_count}) "
                    f"calculated=({calc_avg},{calc_count})"
                )

        print(
            "=== Customer aggregate audit "
            "(userTable.customerRating / totalCustomerReviews) ==="
        )
        customers = conn.execute(
            text(
                """
                SELECT userAppId,
                       customerRating AS stored_rating,
                       totalCustomerReviews AS stored_count
                FROM usertable
                ORDER BY userAppId
                """
            )
        ).mappings().all()

        for customer in customers:
            rows = conn.execute(
                text(
                    """
                    SELECT generalRating
                    FROM customerreviews
                    WHERE ratingReceiverUserAppId = :cid
                    """
                ),
                {"cid": customer["userAppId"]},
            ).mappings().all()
            calc_count = len(rows)
            if calc_count == 0:
                calc_avg = "0.00"
            else:
                calc_avg = _round2(
                    sum(float(r["generalRating"]) for r in rows) / calc_count
                )

            try:
                stored_rating = _round2(float(customer["stored_rating"] or 0))
            except (TypeError, ValueError):
                stored_rating = str(customer["stored_rating"])
            stored_count = int(customer["stored_count"] or 0)
            # Skip noise for users with no reviews and default stored rating
            if calc_count == 0 and stored_count == 0:
                continue
            if stored_rating != calc_avg or stored_count != calc_count:
                mismatches += 1
                print(
                    f"MISMATCH customer={customer['userAppId']} "
                    f"stored=({stored_rating},{stored_count}) "
                    f"calculated=({calc_avg},{calc_count})"
                )

    print(f"AUDIT COMPLETE mismatches={mismatches}")
    print("NOTE: This script does not modify data. Repair requires separate approval.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
