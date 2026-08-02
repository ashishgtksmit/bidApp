#!/usr/bin/env python3
"""PR15 preflight: report duplicate normalized car registrations before unique index.

Rules (must match app_v1.crud.car_manage.normalize_car_registration):
  - trim whitespace
  - uppercase
  - retain ASCII letters and digits only
  - empty normalized result is invalid

Does NOT silently delete, merge, or reassign duplicates.
Exit code 0 when no conflicts; 1 when conflicts or empty/invalid regs exist.
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def normalize_car_registration(raw: str) -> str:
    text = str(raw or "").strip().upper()
    return re.sub(r"[^A-Z0-9]", "", text)


def main() -> int:
    database_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")
    if not database_url:
        # Fall back to app settings if available
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
            database_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")
        except Exception:
            pass

    if not database_url:
        print("ERROR: Set DATABASE_URL (or SQLALCHEMY_DATABASE_URL) to run preflight.")
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    conflicts: dict[str, list[dict]] = defaultdict(list)
    invalid: list[dict] = []

    with engine.connect() as conn:
        # Detect whether normalizedCarRegNo already exists
        cols = {
            row[0]
            for row in conn.execute(text("SHOW COLUMNS FROM cardetails")).fetchall()
        }
        has_norm = "normalizedCarRegNo" in cols
        has_deleted = "isDeleted" in cols

        select_cols = "CARID, userAppId, carRegNo"
        if has_norm:
            select_cols += ", normalizedCarRegNo"
        if has_deleted:
            select_cols += ", isDeleted"

        rows = conn.execute(text(f"SELECT {select_cols} FROM cardetails")).mappings().all()

    for row in rows:
        display = row["carRegNo"]
        if has_norm and row.get("normalizedCarRegNo"):
            normalized = str(row["normalizedCarRegNo"]).strip().upper()
        else:
            normalized = normalize_car_registration(display)

        entry = {
            "CARID": row["CARID"],
            "userAppId": row["userAppId"],
            "carRegNo": display,
            "normalizedCarRegNo": normalized,
            "isDeleted": bool(row["isDeleted"]) if has_deleted else False,
        }

        if not normalized:
            invalid.append(entry)
            continue
        conflicts[normalized].append(entry)

    duplicate_groups = {k: v for k, v in conflicts.items() if len(v) > 1}

    print("=== PR15 normalized registration preflight ===")
    print(f"Rows scanned: {len(rows)}")
    print(f"Invalid/empty normalized registrations: {len(invalid)}")
    print(f"Duplicate normalized groups: {len(duplicate_groups)}")

    if invalid:
        print("\n--- Invalid (empty after normalize) ---")
        for item in invalid:
            print(
                f"  CARID={item['CARID']} userAppId={item['userAppId']} "
                f"carRegNo={item['carRegNo']!r}"
            )

    if duplicate_groups:
        print("\n--- Duplicate conflicts (do not apply unique index) ---")
        for normalized, items in sorted(duplicate_groups.items()):
            print(f"  normalized={normalized}")
            for item in items:
                print(
                    f"    CARID={item['CARID']} userAppId={item['userAppId']} "
                    f"carRegNo={item['carRegNo']!r} isDeleted={item['isDeleted']}"
                )

    if invalid or duplicate_groups:
        print(
            "\nSTOP: Resolve conflicts manually before applying the unique index. "
            "Do not silently delete/merge/reassign."
        )
        return 1

    print("\nOK: No conflicts. Safe to apply PR15 migration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
