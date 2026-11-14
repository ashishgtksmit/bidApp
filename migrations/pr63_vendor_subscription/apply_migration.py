#!/usr/bin/env python3
"""Apply PR63 vendor subscription: three tables, vendor_wallet_recharges.purpose,
and the MONTHLY plan seed (Rs 500 / 30 days).

Default is a dry run (read-only). Pass --apply to execute. Every step is
idempotent; MySQL DDL auto-commits. The seed never overwrites an existing
MONTHLY row (change the price in the table directly).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MIG_DIR.parent / "pr50_sightseeing"))

from _db import column_exists, make_engine, table_exists  # noqa: E402

SCHEMA_SQL = MIG_DIR / "schema_vendor_subscription.sql"
TABLES = ("vendor_subscription_plans", "vendor_subscription_periods", "vendor_subscription_state")
PURPOSE_DDL = "ALTER TABLE vendor_wallet_recharges ADD COLUMN purpose VARCHAR(20) NULL"
SEED_SQL = (
    "INSERT INTO vendor_subscription_plans (code, name, price_paise, duration_days, active) "
    "SELECT 'MONTHLY', 'Monthly', 50000, 30, 1 FROM DUAL "
    "WHERE NOT EXISTS (SELECT 1 FROM vendor_subscription_plans WHERE code = 'MONTHLY')"
)


def _create_statements() -> dict[str, str]:
    body = "\n".join(
        line for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    statements = {}
    for stmt in body.split(";"):
        stmt = stmt.strip()
        match = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", stmt)
        if match:
            statements[match.group(1)] = stmt
    return statements


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    from sqlalchemy import text

    engine = make_engine()
    statements = _create_statements()
    with engine.connect() as conn:
        existing = {table: table_exists(conn, table) for table in TABLES}
        has_recharges = table_exists(conn, "vendor_wallet_recharges")
        has_purpose = has_recharges and column_exists(conn, "vendor_wallet_recharges", "purpose")
        print("Planned changes:")
        for table in TABLES:
            print(f"  {table}: {'exists' if existing[table] else 'CREATE'}")
        if not has_recharges:
            print("  vendor_wallet_recharges: MISSING (deploy the PR48 wallet first)")
        else:
            print(f"  vendor_wallet_recharges.purpose: {'exists' if has_purpose else 'ADD COLUMN'}")
        print("  vendor_subscription_plans MONTHLY seed: insert if missing")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to execute.")
            return 0
        if not has_recharges:
            print("Aborted: vendor_wallet_recharges does not exist.")
            return 1

        for table in TABLES:
            if not existing[table]:
                conn.execute(text(statements[table]))
                print(f"Created {table}.")
        if not has_purpose:
            conn.execute(text(PURPOSE_DDL))
            print("Added vendor_wallet_recharges.purpose.")
        conn.execute(text(SEED_SQL))
        conn.commit()

        plan = conn.execute(
            text(
                "SELECT price_paise, duration_days, active FROM vendor_subscription_plans "
                "WHERE code = 'MONTHLY'"
            )
        ).first()
        print(
            "Verify: "
            + ", ".join(f"{t}={'ok' if table_exists(conn, t) else 'MISSING'}" for t in TABLES)
            + f", purpose={'ok' if column_exists(conn, 'vendor_wallet_recharges', 'purpose') else 'MISSING'}"
            + f", MONTHLY={tuple(plan) if plan else 'MISSING'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
