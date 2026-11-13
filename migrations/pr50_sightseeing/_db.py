"""Shared DB helpers for the PR50 sightseeing migration scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SIGHTSEEING_REQUEST_TYPE_NAME = "Sightseeing"

SPOTS_TABLE = "ob_sightseeing_spots"
DAYS_TABLE = "request_itinerary_days"
DAY_SPOTS_TABLE = "request_itinerary_day_spots"

# WizzRide location_code -> OpenBid location_details.location name candidates.
WIZZRIDE_LOCATION_NAMES = {
    "GANGTOK": ["Gangtok"],
    "DARJEELING": ["Darjeeling"],
    "KALIMPONG": ["Kalimpong"],
    "PELLING": ["Pelling"],
    "NORTH_SIKKIM": ["North Sikkim"],
    "SINGTOM": ["Singtom Tea Estate", "Singtom"],
}


def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        load_dotenv(ROOT / "app_v1" / ".env")
    except Exception:
        pass


def resolve_database_url() -> str | None:
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


def make_engine():
    from sqlalchemy import create_engine

    load_env()
    url = resolve_database_url()
    if not url:
        raise SystemExit("ERROR: Set DATABASE_URL (or DB_* components).")
    connect_args = {}
    ca_path = ROOT / "DigiCertGlobalRootG2.crt.pem"
    if url.startswith("mysql+pymysql") and ca_path.exists():
        connect_args = {"ssl": {"ca": str(ca_path)}}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def table_exists(conn, table: str) -> bool:
    from sqlalchemy import text

    return bool(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def column_exists(conn, table: str, column: str) -> bool:
    from sqlalchemy import text

    return bool(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def map_locations(conn) -> dict[str, int | None]:
    """Return WizzRide location_code -> location_details.LID (None when unmatched)."""
    from sqlalchemy import text

    rows = conn.execute(text("SELECT LID, location FROM location_details")).fetchall()
    by_name: dict[str, list[int]] = {}
    for lid, name in rows:
        by_name.setdefault(str(name).strip().lower(), []).append(int(lid))
    result: dict[str, int | None] = {}
    for code, candidates in WIZZRIDE_LOCATION_NAMES.items():
        lid = None
        for candidate in candidates:
            hits = by_name.get(candidate.lower())
            if hits and len(hits) == 1:
                lid = hits[0]
                break
        result[code] = lid
    return result
