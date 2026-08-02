"""PR18 — post-commit vendor snapshot refresh for preference updates.

Propagation mechanism (chosen):
  After a successful preference mutation commit, FastAPI invokes the existing
  openbid-worker HTTP endpoint ``POST /build_snapshot`` with
  ``{"appid": <jwt_sub>, "flag": "Vendor"}``.

  That reuses the worker's ``fetch_vendor_data`` builder, writes
  ``ws:snapshot:Vendor:{appid}``, and publishes on ``ws_updates`` so
  already-connected WSS clients receive the normal Vendor snapshot shape.

Environment (same convention as openbid-ws):
  * ``WORKER_BASE_URL`` — e.g. ``https://<funcapp>.azurewebsites.net/api``
  * ``BUILD_SNAPSHOT_FUNCTION_KEY`` (preferred) or ``WORKER_FUNCTION_KEY``

Rules:
  * Call only AFTER DB commit
  * Never raise into the preference API (business update stays committed)
  * Log only safe operational metadata (appid length / status), never prefs/JWT
  * Same-value preference replay should skip this hook (caller responsibility)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


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


def request_vendor_snapshot_refresh(user_app_id: str) -> bool:
    """Ask worker to rebuild+publish the Vendor snapshot for one appid.

    Returns True when the worker HTTP call reports success; False otherwise.
    Never raises.
    """
    appid = (user_app_id or "").strip()
    if not appid:
        logger.warning("vendor_snapshot_refresh skipped: empty appid")
        return False

    base = _worker_base_url()
    if not base:
        logger.warning(
            "vendor_snapshot_refresh skipped: WORKER_BASE_URL unset (appid_len=%s)",
            len(appid),
        )
        return False

    url = base.rstrip("/") + "/build_snapshot"
    params = {}
    function_key = _worker_function_key()
    if function_key:
        params["code"] = function_key

    payload = {"appid": appid, "flag": "Vendor"}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, params=params, json=payload)
    except Exception:
        logger.exception(
            "vendor_snapshot_refresh HTTP error (appid_len=%s)",
            len(appid),
        )
        return False

    if resp.status_code != 200:
        logger.warning(
            "vendor_snapshot_refresh non-200 status=%s appid_len=%s",
            resp.status_code,
            len(appid),
        )
        return False

    logger.info(
        "vendor_snapshot_refresh ok appid_len=%s",
        len(appid),
    )
    return True
