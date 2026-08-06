#!/usr/bin/env python3
"""Apply PR39 openbid_domain_outbox migration.

Runs preflight first. Creates table + indexes. Idempotent where practical.
Does not claim production applied.
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

from preflight_domain_event_outbox import main as preflight_main  # noqa: E402

TABLE = "openbid_domain_outbox"

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  eventId CHAR(36) NOT NULL,
  eventType VARCHAR(64) NOT NULL,
  aggregateType VARCHAR(32) NOT NULL,
  aggregateId VARCHAR(64) NOT NULL,
  payload JSON NOT NULL,
  schemaVersion INT NOT NULL,
  occurredAt DATETIME(6) NOT NULL,
  createdAt DATETIME(6) NOT NULL,
  publishedAt DATETIME(6) NULL,
  attemptCount INT NOT NULL DEFAULT 0,
  nextAttemptAt DATETIME(6) NOT NULL,
  lastErrorCode VARCHAR(64) NULL,
  lockedAt DATETIME(6) NULL,
  lockedBy VARCHAR(128) NULL,
  status VARCHAR(16) NOT NULL,
  actorAuthSubjectHash VARCHAR(64) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_openbid_domain_outbox_event_id (eventId),
  KEY ix_outbox_status_next_attempt_id (status, nextAttemptAt, id),
  KEY ix_outbox_locked_at_status (lockedAt, status),
  KEY ix_outbox_event_type_created (eventType, createdAt)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _index_names(conn) -> set[str]:
    from sqlalchemy import text

    rows = conn.execute(text(f"SHOW INDEX FROM {TABLE}")).fetchall()
    names = set()
    for row in rows:
        try:
            names.add(str(row[2]))  # Key_name
        except Exception:
            continue
    return names


def _column_names(conn) -> set[str]:
    from sqlalchemy import text

    rows = conn.execute(text(f"SHOW COLUMNS FROM {TABLE}")).fetchall()
    return {str(r[0]) for r in rows}


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

    print("Running preflight…")
    if preflight_main() != 0:
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    with engine.begin() as conn:
        # Stop safely if incompatible schema already exists
        exists = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": TABLE},
        ).scalar()
        if exists:
            cols = _column_names(conn)
            required = {
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
            missing = required - cols
            if missing:
                print(
                    f"ERROR: incompatible existing {TABLE}; missing {sorted(missing)}. "
                    "Stopping without destructive change."
                )
                return 1
            if "actorAuthSubjectHash" not in cols:
                conn.execute(
                    text(
                        f"ALTER TABLE {TABLE} "
                        "ADD COLUMN actorAuthSubjectHash VARCHAR(64) NULL"
                    )
                )
                print("Added actorAuthSubjectHash column.")
            print(f"{TABLE} already present and compatible.")
        else:
            conn.execute(text(CREATE_SQL))
            print(f"Created table {TABLE}.")

        # Ensure indexes (CREATE TABLE IF NOT EXISTS may have skipped on old partial table)
        names = _index_names(conn)
        index_ddl = {
            "uq_openbid_domain_outbox_event_id": (
                f"ALTER TABLE {TABLE} ADD UNIQUE INDEX "
                "uq_openbid_domain_outbox_event_id (eventId)"
            ),
            "ix_outbox_status_next_attempt_id": (
                f"ALTER TABLE {TABLE} ADD INDEX "
                "ix_outbox_status_next_attempt_id (status, nextAttemptAt, id)"
            ),
            "ix_outbox_locked_at_status": (
                f"ALTER TABLE {TABLE} ADD INDEX "
                "ix_outbox_locked_at_status (lockedAt, status)"
            ),
            "ix_outbox_event_type_created": (
                f"ALTER TABLE {TABLE} ADD INDEX "
                "ix_outbox_event_type_created (eventType, createdAt)"
            ),
        }
        for name, ddl in index_ddl.items():
            if name in names:
                print(f"Index present: {name}")
            else:
                conn.execute(text(ddl))
                print(f"Created index: {name}")

    print("Migration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
