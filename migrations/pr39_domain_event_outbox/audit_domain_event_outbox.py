#!/usr/bin/env python3
"""Audit PR39 openbid_domain_outbox after migration / during ops.

Read-only. Exit 0 when invariants hold; exit 1 otherwise.
Does not claim production execution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TABLE = "openbid_domain_outbox"
ALLOWED_STATUS = ("pending", "published", "dead")


def _resolve_database_url() -> str | None:
    database_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")
    if database_url:
        return database_url
    user = os.getenv("DB_USERNAME")
    password = os.getenv("DB_PASSWORD")
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT", "3306")
    name = os.getenv("DB_NAME")
    if user and password and host and name:
        from urllib.parse import quote_plus

        return f"mysql+pymysql://{user}:{quote_plus(password)}@{host}:{port}/{name}"
    return None


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        load_dotenv(ROOT / "app_v1" / ".env")
    except Exception:
        pass

    database_url = _resolve_database_url()
    if not database_url:
        print("ERROR: Set DATABASE_URL (or DB_* components).")
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    ok = True

    with engine.connect() as conn:
        exists = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": TABLE},
        ).scalar()
        if not exists:
            print(f"FAIL: missing table {TABLE}")
            return 1

        pending = conn.execute(
            text(f"SELECT COUNT(*) FROM {TABLE} WHERE status = 'pending'")
        ).scalar()
        published = conn.execute(
            text(f"SELECT COUNT(*) FROM {TABLE} WHERE status = 'published'")
        ).scalar()
        dead = conn.execute(
            text(f"SELECT COUNT(*) FROM {TABLE} WHERE status = 'dead'")
        ).scalar()
        print(f"pending={pending} published={published} dead={dead}")

        oldest = conn.execute(
            text(
                f"SELECT TIMESTAMPDIFF(SECOND, MIN(createdAt), UTC_TIMESTAMP(6)) "
                f"FROM {TABLE} WHERE status = 'pending'"
            )
        ).scalar()
        print(f"oldest_pending_age_seconds={oldest}")

        dups = conn.execute(
            text(
                f"SELECT eventId, COUNT(*) AS c FROM {TABLE} "
                f"GROUP BY eventId HAVING c > 1 LIMIT 20"
            )
        ).fetchall()
        if dups:
            ok = False
            print(f"FAIL: duplicate eventIds: {dups}")
        else:
            print("OK: no duplicate eventIds")

        invalid_status = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE} "
                f"WHERE status NOT IN ('pending','published','dead')"
            )
        ).scalar()
        if invalid_status:
            ok = False
            print(f"FAIL: invalid status rows={invalid_status}")
        else:
            print("OK: statuses within allowed set")

        null_payload = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE} "
                f"WHERE payload IS NULL OR eventId IS NULL OR eventId = '' "
                f"OR eventType IS NULL OR eventType = '' "
                f"OR aggregateId IS NULL OR aggregateId = ''"
            )
        ).scalar()
        if null_payload:
            ok = False
            print(f"FAIL: null/invalid required fields rows={null_payload}")
        else:
            print("OK: required fields non-null")

        # Bid.created payload shape sample (identifiers only — do not print phones)
        sample = conn.execute(
            text(
                f"SELECT eventType, schemaVersion, "
                f"JSON_EXTRACT(payload, '$.requestId') AS requestId, "
                f"JSON_EXTRACT(payload, '$.bidId') AS bidId "
                f"FROM {TABLE} WHERE eventType = 'bid.created' LIMIT 5"
            )
        ).fetchall()
        print(f"bid_created_sample_count={len(sample)}")
        for row in sample:
            print(
                f"  sample eventType={row[0]} schemaVersion={row[1]} "
                f"requestId={row[2]} bidId={row[3]}"
            )

    if not ok:
        print("Audit FAILED.")
        return 1
    print("Audit OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
