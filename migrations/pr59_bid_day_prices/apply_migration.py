#!/usr/bin/env python3
"""Apply PR59 per-day bid prices: bid_day_prices table + biddetails.pricingMode.

Default is a dry run (read-only). Pass --apply to execute. Both steps are
idempotent; MySQL DDL auto-commits.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MIG_DIR.parent / "pr50_sightseeing"))

from _db import column_exists, make_engine, table_exists  # noqa: E402

TABLE = "bid_day_prices"
SCHEMA_SQL = MIG_DIR / "schema_bid_day_prices.sql"
PRICING_MODE_DDL = "ALTER TABLE biddetails ADD COLUMN pricingMode VARCHAR(10) NULL"


def _create_table_sql() -> str:
    body = "\n".join(
        line for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    return body.strip().rstrip(";")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    engine = make_engine()
    with engine.connect() as conn:
        has_table = table_exists(conn, TABLE)
        has_column = column_exists(conn, "biddetails", "pricingMode")
        print("Planned changes:")
        print(f"  {TABLE}: {'exists' if has_table else 'CREATE'}")
        print(f"  biddetails.pricingMode: {'exists' if has_column else 'ADD COLUMN'}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to execute.")
            return 0

        if not has_table:
            conn.execute(text(_create_table_sql()))
            print(f"Created {TABLE}.")
        if not has_column:
            conn.execute(text(PRICING_MODE_DDL))
            print("Added biddetails.pricingMode.")
        conn.commit()

        print(
            "Verify: "
            f"{TABLE}={'ok' if table_exists(conn, TABLE) else 'MISSING'}, "
            f"pricingMode={'ok' if column_exists(conn, 'biddetails', 'pricingMode') else 'MISSING'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
