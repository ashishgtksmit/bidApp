#!/usr/bin/env python3
"""Preflight for PR39 openbid_domain_outbox.

Read-only. Exit 0 when safe to apply; exit 1 on unsupported schema or hazards.
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
REQUIRED_COLUMNS = {
    "id",
    "eventId",
    "eventType",
    "aggregateType",
    "aggregateId",
    "payload",
    "schemaVersion",
    "occurredAt",
    "createdAt",
    "publishedAt",
    "attemptCount",
    "nextAttemptAt",
    "lastErrorCode",
    "lockedAt",
    "lockedBy",
    "status",
}


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        load_dotenv(ROOT / "app_v1" / ".env")
    except Exception:
        pass

    database_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")
    if not database_url:
        # Fall back to component env used by app_v1/database.py
        user = os.getenv("DB_USERNAME")
        password = os.getenv("DB_PASSWORD")
        host = os.getenv("DB_HOST")
        port = os.getenv("DB_PORT", "3306")
        name = os.getenv("DB_NAME")
        if user and password and host and name:
            from urllib.parse import quote_plus

            database_url = (
                f"mysql+pymysql://{user}:{quote_plus(password)}@{host}:{port}/{name}"
            )
        else:
            print("ERROR: Set DATABASE_URL (or DB_* components).")
            return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    blocking = False

    with engine.connect() as conn:
        # MySQL version / locking semantics
        try:
            ver = conn.execute(text("SELECT VERSION()")).scalar()
            print(f"OK: MySQL VERSION()={ver}")
            # SKIP LOCKED requires MySQL 8.0+
            major = 0
            try:
                major = int(str(ver).split(".", 1)[0])
            except Exception:
                pass
            if major < 8:
                print("BLOCKING: MySQL < 8.0 — FOR UPDATE SKIP LOCKED unsupported")
                blocking = True
            else:
                print("OK: MySQL major version supports SKIP LOCKED")
        except Exception as exc:
            print(f"BLOCKING: cannot read MySQL version: {exc}")
            return 1

        # JSON support probe
        try:
            conn.execute(text("SELECT CAST('{\"a\":1}' AS JSON)"))
            print("OK: MySQL JSON support present")
        except Exception as exc:
            print(f"BLOCKING: MySQL JSON unsupported: {exc}")
            blocking = True

        # Privileges: need CREATE / INDEX on schema
        try:
            grants = conn.execute(text("SHOW GRANTS")).fetchall()
            grant_text = " ".join(str(g[0]) for g in grants).upper()
            print(f"INFO: grants_count={len(grants)}")
            if "ALL PRIVILEGES" not in grant_text and "CREATE" not in grant_text:
                print("WARNING: CREATE privilege not obvious in SHOW GRANTS")
            if "ALL PRIVILEGES" not in grant_text and "INDEX" not in grant_text:
                print("WARNING: INDEX privilege not obvious in SHOW GRANTS")
        except Exception as exc:
            print(f"WARNING: SHOW GRANTS unavailable: {exc}")

        # Existing table conflict check
        try:
            exists = conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = :t"
                ),
                {"t": TABLE},
            ).scalar()
        except Exception as exc:
            print(f"BLOCKING: cannot inspect information_schema: {exc}")
            return 1

        if not exists:
            print(f"INFO: table absent (will be created): {TABLE}")
        else:
            print(f"INFO: table already present: {TABLE}")
            cols = conn.execute(text(f"SHOW COLUMNS FROM {TABLE}")).fetchall()
            col_names = {str(c[0]) for c in cols}
            missing = REQUIRED_COLUMNS - col_names
            # actorAuthSubjectHash is optional additive column
            unexpected_critical = False
            if missing:
                # If only optional actor column missing from REQUIRED — actor not in REQUIRED
                print(f"BLOCKING: incompatible existing table; missing columns: {sorted(missing)}")
                blocking = True
                unexpected_critical = True
            # status column type / length sanity
            status_meta = next((c for c in cols if str(c[0]) == "status"), None)
            if status_meta is not None:
                print(f"OK: status column present → {status_meta}")
            if not unexpected_critical and not missing:
                print("OK: existing table columns compatible with PR39 outbox")

            # Index creation capability is assumed if table exists and ALTER INDEX grant present
            print("INFO: indexes will be created idempotently by apply if missing")

        print("INFO: no destructive DROP/ALTER of unrelated tables planned")

    if blocking:
        print("Preflight FAILED.")
        return 1
    print("Preflight OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
