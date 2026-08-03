#!/usr/bin/env python3
"""PR19 preflight: report duplicate RID groups in review tables.

Exits non-zero when duplicate RID conflicts exist.
Does NOT delete or merge rows.
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
    conflicts = 0

    with engine.connect() as conn:
        print("=== Duplicate vendorreviews.RID groups ===")
        vendor_dupes = conn.execute(
            text(
                """
                SELECT RID, COUNT(*) AS cnt, GROUP_CONCAT(VRID ORDER BY VRID) AS row_ids
                FROM vendorreviews
                GROUP BY RID
                HAVING COUNT(*) > 1
                ORDER BY RID
                """
            )
        ).mappings().all()
        if not vendor_dupes:
            print("OK: no duplicate vendorreviews RID groups")
        else:
            conflicts += len(vendor_dupes)
            for row in vendor_dupes:
                print(
                    f"CONFLICT RID={row['RID']} count={row['cnt']} "
                    f"VRID_list={row['row_ids']}"
                )

        print("=== Duplicate customerreviews.RID groups ===")
        customer_dupes = conn.execute(
            text(
                """
                SELECT RID, COUNT(*) AS cnt, GROUP_CONCAT(CR ORDER BY CR) AS row_ids
                FROM customerreviews
                GROUP BY RID
                HAVING COUNT(*) > 1
                ORDER BY RID
                """
            )
        ).mappings().all()
        if not customer_dupes:
            print("OK: no duplicate customerreviews RID groups")
        else:
            conflicts += len(customer_dupes)
            for row in customer_dupes:
                print(
                    f"CONFLICT RID={row['RID']} count={row['cnt']} "
                    f"CR_list={row['row_ids']}"
                )

    if conflicts:
        print(f"FAIL: {conflicts} duplicate RID group(s). Do not apply unique indexes.")
        return 1

    print("PASS: no duplicate RID conflicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
