"""Post-commit WSS snapshot refresh via openbid-worker ``POST /build_snapshot``.

PR18 introduced Vendor-only preference refresh. PR19 generalizes the helper so
review mutations can refresh the rated user's Vendor or Customer snapshot.

Payload:
  ``{"appid": "<userAppId>", "flag": "Vendor"|"Customer"}``

Environment (same convention as openbid-ws):
  * ``WORKER_BASE_URL`` — e.g. ``https://<funcapp>.azurewebsites.net/api``
  * ``BUILD_SNAPSHOT_FUNCTION_KEY`` (preferred) or ``WORKER_FUNCTION_KEY``

Rules:
  * Call only AFTER DB commit
  * Never raise into the business API (mutation stays committed)
  * Log only safe operational metadata (appid length / status / flag)
  * Never log review text, phones as values, JWTs, or rating payloads
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_FLAGS = frozenset({"Vendor", "Customer"})


def _worker_base_url() -> Optional[str]:
    raw = os.getenv("WORKER_BASE_URL")
    if not raw:
        return None
    trimmed = raw.strip()
    return trimmed or None


def _worker_function_key() -> Optional[str]:
    for key in ("BUILD_SNAPSHOT_FUNCTION_KEY", "WORKER_FUNCTION_KEY"):
        raw = os.getenv(key)
        if raw and raw.strip():
            return raw.strip()
    return None


def request_snapshot_refresh(user_app_id: str, *, flag: str = "Vendor") -> bool:
    """Ask worker to rebuild+publish one user's snapshot.

    Returns True when the worker HTTP call reports success; False otherwise.
    Never raises.
    """
    appid = (user_app_id or "").strip()
    snapshot_flag = (flag or "").strip()
    if not appid:
        logger.warning("snapshot_refresh skipped: empty appid")
        return False
    if snapshot_flag not in _ALLOWED_FLAGS:
        logger.warning(
            "snapshot_refresh skipped: invalid flag (appid_len=%s)",
            len(appid),
        )
        return False

    base = _worker_base_url()
    if not base:
        logger.warning(
            "snapshot_refresh skipped: WORKER_BASE_URL unset (flag=%s appid_len=%s)",
            snapshot_flag,
            len(appid),
        )
        return False

    url = base.rstrip("/") + "/build_snapshot"
    params = {}
    function_key = _worker_function_key()
    if function_key:
        params["code"] = function_key

    payload = {"appid": appid, "flag": snapshot_flag}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, params=params, json=payload)
    except Exception:
        logger.exception(
            "snapshot_refresh HTTP error (flag=%s appid_len=%s)",
            snapshot_flag,
            len(appid),
        )
        return False

    if resp.status_code != 200:
        logger.warning(
            "snapshot_refresh non-200 status=%s flag=%s appid_len=%s",
            resp.status_code,
            snapshot_flag,
            len(appid),
        )
        return False

    logger.info(
        "snapshot_refresh ok flag=%s appid_len=%s",
        snapshot_flag,
        len(appid),
    )
    return True


def request_vendor_snapshot_refresh(user_app_id: str) -> bool:
    """PR18-compatible Vendor snapshot refresh wrapper."""
    return request_snapshot_refresh(user_app_id, flag="Vendor")
