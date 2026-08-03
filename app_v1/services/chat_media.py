"""PR28 — authenticated chat media upload and pre-commit cleanup.

Authorization reuses PR26 peer and PR27 support relationship rules without
requiring a committed RTDB message at upload time. Cleanup verifies the RTDB
message is absent before deleting the deterministic blob.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from ..crud.admin_number import resolve_support_identity
from ..models.user_table import User
from ..services.chat_notifications import (
    has_eligible_customer_vendor_relationship,
    parse_admin_thread_suffix,
    validate_message_id as _validate_message_id_notify,
)
from ..utils.firebase_realtime import (
    ChatDatabaseUnavailable,
    ChatMessageReadError,
    get_chat_message,
)
from ..utils.image import (
    ChatDocsStorageError,
    ChatMediaImageError,
    chat_docs_delete_blob,
    chat_docs_head_metadata,
    chat_docs_upload_bytes,
    decode_chat_media_payload,
    validate_chat_media_image_bytes,
)
from ..utils.rate_limit import enforce_rate_limit

_logger = logging.getLogger(__name__)

_SENDER_MAX = int(os.getenv("RATE_LIMIT_CHAT_MEDIA_SENDER_PER_MIN", "20"))
_PAIR_MAX = int(os.getenv("RATE_LIMIT_CHAT_MEDIA_PAIR_PER_MIN", "15"))
_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_CHAT_MEDIA_WINDOW_SECONDS", "60"))
_MESSAGE_EVENT_WINDOW_SECONDS = int(
    os.getenv("RATE_LIMIT_CHAT_MEDIA_MESSAGE_WINDOW_SECONDS", str(30 * 24 * 3600))
)

_THREAD_ID_MAX = 128


class ChatMediaError(Exception):
    """Typed chat-media failure with HTTP status + stable detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _is_tombstone_user_app_id(user_app_id: Optional[str]) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()


def _hash_part(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def thread_hash(thread_id: str) -> str:
    """SHA-256 prefix of the complete thread ID (never raw phones in blob path)."""
    return _hash_part(thread_id)


def _load_user(db: Session, user_app_id: str) -> Optional[User]:
    return db.query(User).filter(User.userAppId == user_app_id).first()


def validate_chat_media_message_id(message_id: str) -> None:
    """Strict Firebase-compatible messageId (reject path separators / traversal)."""
    mid = str(message_id or "").strip()
    if not mid or len(mid) > 128:
        raise ChatMediaError(422, "INVALID_MESSAGE_ID")
    if any(ch in mid for ch in ("/", "\\", ".", "#", "$", "[", "]")):
        raise ChatMediaError(422, "INVALID_MESSAGE_ID")
    if any(ch.isspace() or ord(ch) < 32 for ch in mid):
        raise ChatMediaError(422, "INVALID_MESSAGE_ID")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", mid):
        raise ChatMediaError(422, "INVALID_MESSAGE_ID")
    # Keep parity with notification push-id rules.
    try:
        _validate_message_id_notify(mid)
    except Exception as exc:
        raise ChatMediaError(422, "INVALID_MESSAGE_ID") from exc


def validate_thread_id_bounds(thread_id: str) -> str:
    tid = str(thread_id or "").strip()
    if not tid or len(tid) > _THREAD_ID_MAX:
        raise ChatMediaError(422, "INVALID_THREAD_FORMAT")
    if any(ch in tid for ch in ("/", "\\", "#", "$", "[", "]")):
        raise ChatMediaError(422, "INVALID_THREAD_FORMAT")
    return tid


def _require_live_unlocked_sender(db: Session, jwt_sub: str) -> User:
    sender_id = str(jwt_sub or "").strip()
    if not sender_id:
        raise ChatMediaError(401, "Not authenticated")
    sender = _load_user(db, sender_id)
    if sender is None:
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    if _is_tombstone_user_app_id(getattr(sender, "userAppId", None)):
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    if bool(getattr(sender, "lockApp", False)):
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    return sender


def _parse_peer_participants(thread_id: str) -> Tuple[str, str]:
    if not re.fullmatch(r"\d+-\d+", thread_id):
        raise ChatMediaError(422, "INVALID_THREAD_FORMAT")
    left, right = thread_id.split("-", 1)
    if not left or not right or left == right:
        raise ChatMediaError(422, "INVALID_THREAD_FORMAT")
    # Canonical ordering must match expected_peer_thread_id.
    try:
        a_int = int(left)
        b_int = int(right)
    except ValueError as exc:
        raise ChatMediaError(422, "INVALID_THREAD_FORMAT") from exc
    if a_int < b_int:
        if thread_id != f"{left}-{right}":
            raise ChatMediaError(422, "INVALID_THREAD_FORMAT")
        return left, right
    if a_int > b_int:
        # Thread must already be sorted smaller-larger.
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    raise ChatMediaError(422, "INVALID_THREAD_FORMAT")


def authorize_peer_media_upload(
    db: Session,
    *,
    sender_id: str,
    thread_id: str,
) -> str:
    """
    Authorize peer chat media without requiring an RTDB message.

    Returns the peer userAppId.
    """
    left, right = _parse_peer_participants(thread_id)
    if sender_id not in (left, right):
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    peer_id = right if sender_id == left else left
    if not has_eligible_customer_vendor_relationship(
        db, user_a=sender_id, user_b=peer_id
    ):
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    return peer_id


def authorize_support_media_upload(
    db: Session,
    *,
    sender_id: str,
    thread_id: str,
) -> str:
    """
    Authorize support chat media without requiring an RTDB message.

    Returns a stable peer/support counterpart id for rate-limit bucketing.
    """
    identity = resolve_support_identity(db)
    if not identity.available or not identity.support_user_app_id:
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")

    support_id = identity.support_user_app_id
    suffix = parse_admin_thread_suffix(thread_id)
    if suffix is None:
        raise ChatMediaError(422, "INVALID_THREAD_FORMAT")

    user_to_support = (
        sender_id != support_id
        and suffix == sender_id
        and thread_id == f"admin-{sender_id}"
    )
    support_to_user = (
        sender_id == support_id
        and bool(suffix)
        and suffix != support_id
        and thread_id == f"admin-{suffix}"
    )

    if not user_to_support and not support_to_user:
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")

    if user_to_support:
        return support_id

    # support → user: recipient must exist (live resolution for identity).
    recipient = _load_user(db, suffix)
    if recipient is None:
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    if _is_tombstone_user_app_id(getattr(recipient, "userAppId", None)):
        raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
    return suffix


def classify_and_authorize_thread(
    db: Session,
    *,
    sender_id: str,
    thread_id: str,
) -> str:
    """Classify peer vs support and authorize. Returns counterpart id."""
    tid = validate_thread_id_bounds(thread_id)
    if tid.lower().startswith("admin-"):
        return authorize_support_media_upload(
            db, sender_id=sender_id, thread_id=tid
        )
    if re.fullmatch(r"\d+-\d+", tid):
        return authorize_peer_media_upload(
            db, sender_id=sender_id, thread_id=tid
        )
    raise ChatMediaError(422, "INVALID_THREAD_FORMAT")


def _blob_relative_path(thread_id: str, message_id: str, ext: str) -> str:
    return f"chat/{thread_hash(thread_id)}/{message_id}.{ext}"


def _safe_display_filename(message_id: str, ext: str) -> str:
    return f"{message_id}.{ext}"


def _apply_sender_pair_rate_limits(
    db: Session,
    *,
    sender_id: str,
    thread_id: str,
) -> None:
    """Apply before storage. Does not run on authorization failures."""
    sender_limited = enforce_rate_limit(
        db,
        bucket_key=f"chat_media:sender:{_hash_part(sender_id)}",
        max_hits=_SENDER_MAX,
        window_seconds=_WINDOW_SECONDS,
    )
    if sender_limited is not None:
        raise ChatMediaError(429, "CHAT_MEDIA_RATE_LIMITED")

    pair_limited = enforce_rate_limit(
        db,
        bucket_key=(
            f"chat_media:pair:{_hash_part(sender_id)}:{_hash_part(thread_id)}"
        ),
        max_hits=_PAIR_MAX,
        window_seconds=_WINDOW_SECONDS,
    )
    if pair_limited is not None:
        raise ChatMediaError(429, "CHAT_MEDIA_RATE_LIMITED")


def _consume_message_event_rate_limit(
    db: Session,
    *,
    thread_id: str,
    message_id: str,
) -> None:
    """
    Record one logical upload event after a successful blob create.

    Applied after storage so a failed upload does not block same-messageId retry.
    Idempotent same-content reconciliation returns earlier via blob HEAD.
    """
    event_limited = enforce_rate_limit(
        db,
        bucket_key=(
            f"chat_media:event:{_hash_part(thread_id)}:{_hash_part(message_id)}"
        ),
        max_hits=1,
        window_seconds=_MESSAGE_EVENT_WINDOW_SECONDS,
    )
    if event_limited is not None:
        # Blob was just created; treat as soft — caller already has URL.
        # Do not raise; duplicate create is prevented by path conflict checks.
        return


def _reconcile_existing_blob(
    *,
    relative_path: str,
    content_digest: str,
    uploader_hash: str,
    thread_hash_value: str,
    message_id: str,
    mime: str,
    size_bytes: int,
) -> Optional[Dict[str, Any]]:
    """
    If blob exists with matching binding+digest → success dict.
    If blob exists with different content → raise 409.
    If missing → None.
    """
    try:
        meta = chat_docs_head_metadata(relative_path)
    except ChatDocsStorageError as exc:
        raise ChatMediaError(500, "CHAT_MEDIA_UPLOAD_FAILED") from exc

    if meta is None:
        return None

    existing_digest = (meta.get("contentsha256") or "").lower()
    existing_uploader = (meta.get("uploaderhash") or "").lower()
    existing_thread = (meta.get("threadhash") or "").lower()
    existing_mid = meta.get("messageid") or ""

    binding_ok = (
        existing_digest == content_digest.lower()
        and existing_uploader == uploader_hash.lower()
        and existing_thread == thread_hash_value.lower()
        and existing_mid == message_id
    )
    if binding_ok:
        from ..utils.image import chat_docs_blob_public_url

        return {
            "message": "UPLOADED",
            "mediaUrl": chat_docs_blob_public_url(relative_path),
            "mimeType": mime,
            "fileName": _safe_display_filename(
                message_id, "jpg" if mime == "image/jpeg" else "png"
            ),
            "sizeBytes": size_bytes,
        }

    # Same path occupied by different content / different uploader binding.
    raise ChatMediaError(409, "CHAT_MEDIA_CONFLICT")


def upload_chat_media(
    db: Session,
    *,
    jwt_sub: str,
    thread_id: str,
    message_id: str,
    media_type: str,
    content: str,
    file_name: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Authorize, validate, and upload one PHOTO to Azure chat-docs.

    Order: authenticate sender → authorize thread → validate messageId →
    decode/validate image → reconcile idempotency → rate limit → upload.
    """
    _ = file_name  # optional non-authoritative hint; never used in blob path
    _ = mime_type  # optional hint; server re-detects from bytes

    if str(media_type or "").strip().upper() != "PHOTO":
        raise ChatMediaError(422, "UNSUPPORTED_CHAT_MEDIA_TYPE")

    sender = _require_live_unlocked_sender(db, jwt_sub)
    sender_id = str(getattr(sender, "userAppId", "") or "").strip()

    tid = validate_thread_id_bounds(thread_id)
    classify_and_authorize_thread(db, sender_id=sender_id, thread_id=tid)
    validate_chat_media_message_id(message_id)

    # Decode/validate after authorization (avoid processing for strangers).
    try:
        binary, claimed = decode_chat_media_payload(content)
        mime, ext = validate_chat_media_image_bytes(binary, claimed)
    except ChatMediaImageError as exc:
        if exc.code == "CHAT_MEDIA_TOO_LARGE":
            raise ChatMediaError(413, "CHAT_MEDIA_TOO_LARGE") from exc
        if exc.code == "UNSUPPORTED_CHAT_MEDIA_TYPE":
            raise ChatMediaError(415, "UNSUPPORTED_CHAT_MEDIA_TYPE") from exc
        raise ChatMediaError(422, "INVALID_CHAT_MEDIA") from exc

    content_digest = hashlib.sha256(binary).hexdigest()
    thash = thread_hash(tid)
    uploader_hash = _hash_part(sender_id)
    relative_path = _blob_relative_path(tid, message_id, ext)

    reconciled = _reconcile_existing_blob(
        relative_path=relative_path,
        content_digest=content_digest,
        uploader_hash=uploader_hash,
        thread_hash_value=thash,
        message_id=message_id,
        mime=mime,
        size_bytes=len(binary),
    )
    if reconciled is not None:
        _logger.info(
            "chat_media_event category=idempotent_hit mime=%s size_bucket=%s",
            "jpeg" if mime == "image/jpeg" else "png",
            _size_bucket(len(binary)),
        )
        return reconciled

    # Rate limits only when creating a new blob (after auth + image validation).
    _apply_sender_pair_rate_limits(db, sender_id=sender_id, thread_id=tid)

    metadata = {
        "messageid": message_id,
        "threadhash": thash,
        "uploaderhash": uploader_hash,
        "contentsha256": content_digest,
        "mimetype": mime,
    }

    try:
        media_url = chat_docs_upload_bytes(
            relative_blob_path=relative_path,
            content=binary,
            content_type=mime,
            metadata=metadata,
        )
    except ChatDocsStorageError as exc:
        _logger.info(
            "chat_media_event category=upload_provider_failure code=%s",
            type(exc).__name__,
        )
        raise ChatMediaError(500, "CHAT_MEDIA_UPLOAD_FAILED") from exc

    _consume_message_event_rate_limit(
        db, thread_id=tid, message_id=message_id
    )

    _logger.info(
        "chat_media_event category=uploaded mime=%s size_bucket=%s thread_hash=%s",
        "jpeg" if mime == "image/jpeg" else "png",
        _size_bucket(len(binary)),
        thash,
    )

    return {
        "message": "UPLOADED",
        "mediaUrl": media_url,
        "mimeType": mime,
        "fileName": _safe_display_filename(message_id, ext),
        "sizeBytes": len(binary),
    }


def cleanup_chat_media(
    db: Session,
    *,
    jwt_sub: str,
    thread_id: str,
    message_id: str,
) -> Dict[str, str]:
    """
    Compensation delete when upload succeeded but RTDB commit failed.

    Re-authorizes thread, verifies RTDB message absence, deletes deterministic
    blob only. Not a user-facing message/media deletion API.
    """
    sender = _require_live_unlocked_sender(db, jwt_sub)
    sender_id = str(getattr(sender, "userAppId", "") or "").strip()

    tid = validate_thread_id_bounds(thread_id)
    classify_and_authorize_thread(db, sender_id=sender_id, thread_id=tid)
    validate_chat_media_message_id(message_id)

    try:
        payload = get_chat_message(thread_id=tid, message_id=message_id)
    except ChatDatabaseUnavailable as exc:
        raise ChatMediaError(503, "CHAT_DATABASE_UNAVAILABLE") from exc
    except ChatMessageReadError as exc:
        raise ChatMediaError(503, "CHAT_DATABASE_UNAVAILABLE") from exc

    if payload is not None and not payload.get("__non_object__"):
        raise ChatMediaError(409, "CHAT_MEDIA_ALREADY_COMMITTED")
    if payload is not None and payload.get("__non_object__"):
        # Unexpected scalar at message path — treat as committed for safety.
        raise ChatMediaError(409, "CHAT_MEDIA_ALREADY_COMMITTED")

    # Try both extensions; only the authorized deterministic paths.
    deleted_any = False
    last_error: Optional[Exception] = None
    for ext in ("jpg", "png"):
        relative_path = _blob_relative_path(tid, message_id, ext)
        try:
            meta = chat_docs_head_metadata(relative_path)
        except ChatDocsStorageError as exc:
            last_error = exc
            continue

        if meta is None:
            continue

        # Only delete when metadata binds to this uploader+thread+message.
        if (meta.get("uploaderhash") or "").lower() != _hash_part(sender_id).lower():
            raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
        if (meta.get("threadhash") or "").lower() != thread_hash(tid).lower():
            raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")
        if (meta.get("messageid") or "") != message_id:
            raise ChatMediaError(403, "CHAT_MEDIA_NOT_ALLOWED")

        try:
            chat_docs_delete_blob(relative_path)
            deleted_any = True
        except ChatDocsStorageError as exc:
            last_error = exc

    if last_error is not None and not deleted_any:
        # Distinguish: if both missing, success; if provider failed on existing, error.
        _logger.info(
            "chat_media_event category=cleanup_provider_failure code=%s",
            type(last_error).__name__,
        )
        raise ChatMediaError(500, "CHAT_MEDIA_CLEANUP_FAILED") from last_error

    _logger.info(
        "chat_media_event category=cleanup_deleted thread_hash=%s",
        thread_hash(tid),
    )
    return {"message": "DELETED"}


def _size_bucket(size_bytes: int) -> str:
    if size_bytes < 100_000:
        return "lt_100kb"
    if size_bytes < 500_000:
        return "lt_500kb"
    if size_bytes < 1_000_000:
        return "lt_1mb"
    if size_bytes <= 2_000_000:
        return "lte_2mb"
    return "gt_2mb"
