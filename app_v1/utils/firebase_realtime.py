"""PR26 — Firebase Admin Realtime Database read helpers for chat verification.

Reads a single committed chat message path only. Never logs message content,
credentials, or service-account JSON.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import fcm as fcm_utils


class ChatDatabaseUnavailable(Exception):
    """RTDB configuration missing or Admin SDK unavailable."""


class ChatMessageReadError(Exception):
    """Provider/read failure while loading a chat message."""


def _database_url() -> str:
    return (os.getenv("FIREBASE_DATABASE_URL") or "").strip()


def ensure_firebase_database_configured() -> str:
    """Return FIREBASE_DATABASE_URL or raise ChatDatabaseUnavailable."""
    url = _database_url()
    if not url:
        raise ChatDatabaseUnavailable("FIREBASE_DATABASE_URL is not configured")
    return url


def get_chat_message(
    *,
    thread_id: str,
    message_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Read ``Chats/{threadId}/{messageId}`` via Firebase Admin.

    Returns:
        The message dict when present, or None when the path is empty/missing.

    Raises:
        ChatDatabaseUnavailable: missing URL / service account / init failure.
        ChatMessageReadError: provider read failure (safe; no provider text).
    """
    database_url = ensure_firebase_database_configured()
    thread_id = (thread_id or "").strip()
    message_id = (message_id or "").strip()
    if not thread_id or not message_id:
        return None

    try:
        # Reuse the shared Admin app (messaging + RTDB same OpenBid project).
        fcm_utils._get_firebase_admin_app()
    except ValueError:
        raise ChatDatabaseUnavailable("Firebase Admin is not configured") from None
    except Exception:
        raise ChatDatabaseUnavailable("Firebase Admin initialization failed") from None

    try:
        from firebase_admin import db
    except Exception:
        raise ChatDatabaseUnavailable("Firebase Admin db module unavailable") from None

    path = f"Chats/{thread_id}/{message_id}"
    try:
        # Pass url explicitly so RTDB works even if the default app was
        # previously initialized without databaseURL (messaging-only callers).
        snapshot = db.reference(path, url=database_url).get()
    except ChatDatabaseUnavailable:
        raise
    except Exception:
        raise ChatMessageReadError("Failed to read chat message") from None

    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        # Caller maps non-object payloads to a safe validation error.
        return {"__non_object__": True, "value": snapshot}
    return snapshot
