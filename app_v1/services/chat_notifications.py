"""PR26/PR27 — chat push notification dispatch (server-owned).

Flutter supplies only threadId + messageId. FastAPI verifies the committed
RTDB message, classifies peer vs support threads, authorizes, derives
recipient and template, and sends FCM. Never returns tokens/phones/message
content.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..crud.admin_number import resolve_support_identity
from ..models.request_table import Request
from ..models.user_table import User
from ..utils.firebase_realtime import (
    ChatDatabaseUnavailable,
    ChatMessageReadError,
    get_chat_message,
)
from ..utils.fcm import send_notification_to_token
from ..utils.rate_limit import enforce_rate_limit

# Lifecycle allow-list for customer↔vendor chat push (approved PR26).
_ALLOWED_CHAT_STATUSES = (
    "BID - CONFIRMED",
    "REQUEST - CONFIRMED",
)

_CHAT_DEEP_LINK = "//Chat_Main_Page"
_PREVIEW_MAX_CHARS = 80

# Peer rate / idempotency (DB-backed api_rate_limit_buckets).
_SENDER_MAX = int(os.getenv("RATE_LIMIT_CHAT_NOTIFICATION_SENDER_PER_MIN", "30"))
_PAIR_MAX = int(os.getenv("RATE_LIMIT_CHAT_NOTIFICATION_PAIR_PER_MIN", "20"))
_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_CHAT_NOTIFICATION_WINDOW_SECONDS", "60"))
_MESSAGE_EVENT_WINDOW_SECONDS = int(
    os.getenv("RATE_LIMIT_CHAT_NOTIFICATION_MESSAGE_WINDOW_SECONDS", str(30 * 24 * 3600))
)

# Support rate limits (PR27).
_SUPPORT_USER_SENDER_MAX = int(
    os.getenv("RATE_LIMIT_SUPPORT_CHAT_USER_SENDER_PER_MIN", "20")
)
_SUPPORT_USER_PAIR_MAX = int(
    os.getenv("RATE_LIMIT_SUPPORT_CHAT_USER_PAIR_PER_MIN", "15")
)
_SUPPORT_OP_SENDER_MAX = int(
    os.getenv("RATE_LIMIT_SUPPORT_CHAT_OPERATOR_PER_MIN", "60")
)
_SUPPORT_OP_PAIR_MAX = int(
    os.getenv("RATE_LIMIT_SUPPORT_CHAT_OPERATOR_PAIR_PER_MIN", "20")
)

_SUPPORT_USER_TITLE = "New Support Message"
_SUPPORT_TO_USER_TITLE = "OpenBid Support"


class ChatNotificationError(Exception):
    """Typed chat-notification failure with HTTP status + stable detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _is_tombstone_user_app_id(user_app_id: Optional[str]) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()


def _clean_token(token: Optional[str]) -> str:
    if not token:
        return ""
    return str(token).strip()


def _hash_bucket_part(value: str) -> str:
    """Stable short hash for rate-limit buckets (avoids raw phone diagnostics)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def sanitize_text_preview(text: Optional[str]) -> str:
    """Sanitize RTDB text for FCM body preview (max 80 Unicode chars)."""
    if text is None:
        return ""
    raw = str(text)
    raw = (
        raw.replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )
    cleaned_chars = []
    for ch in raw:
        category = unicodedata.category(ch)
        if category.startswith("C"):
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars)
    cleaned = re.sub(r" +", " ", cleaned).strip()
    if len(cleaned) > _PREVIEW_MAX_CHARS:
        cleaned = cleaned[:_PREVIEW_MAX_CHARS]
    return cleaned


def build_notification_body(*, message_type: Optional[str], text: Optional[str]) -> str:
    """Peer (PR26) server-owned body templates. Never includes media URLs or contacts."""
    kind = str(message_type or "").strip().lower()
    if kind == "photo":
        return "Sent you an image"
    if kind == "contact":
        return "Shared a contact"
    if kind in {"file", "document", "doc", "pdf", "video", "audio"}:
        return "Sent you a file"
    if kind == "text" or kind == "":
        preview = sanitize_text_preview(text)
        return preview if preview else "Sent you a message"
    return "Sent you a message"


def build_support_notification_body(*, message_type: Optional[str]) -> str:
    """Support (PR27) fixed privacy-preserving templates — never copies message text."""
    kind = str(message_type or "").strip().lower()
    if kind == "photo":
        return "Sent you an image"
    if kind == "contact":
        return "Shared a contact"
    if kind in {"file", "document", "doc", "pdf", "video", "audio"}:
        return "Sent you a file"
    return "Sent you a message"


def build_notification_title(sender_display_name: str) -> str:
    name = sanitize_text_preview(sender_display_name) or "OpenBid User"
    if len(name) > 80:
        name = name[:80]
    return f"New Message from {name}"


def expected_peer_thread_id(phone_a: str, phone_b: str) -> str:
    """Peer thread key: {smallerPhone}-{largerPhone} via numeric ordering."""
    a = str(phone_a or "").strip()
    b = str(phone_b or "").strip()
    if not a or not b:
        raise ChatNotificationError(422, "INVALID_THREAD_FORMAT")
    try:
        a_int = int(a)
        b_int = int(b)
    except ValueError as exc:
        raise ChatNotificationError(422, "INVALID_THREAD_FORMAT") from exc
    if a_int < b_int:
        return f"{a}-{b}"
    return f"{b}-{a}"


def validate_peer_thread_id(thread_id: str, sender: str, receiver: str) -> None:
    thread_id = str(thread_id or "").strip()
    if thread_id.lower().startswith("admin-"):
        # Peer validator must never authorize support threads.
        raise ChatNotificationError(403, "CHAT_NOTIFICATION_NOT_ALLOWED")
    if not re.fullmatch(r"\d+-\d+", thread_id):
        raise ChatNotificationError(422, "INVALID_THREAD_FORMAT")
    expected = expected_peer_thread_id(sender, receiver)
    if thread_id != expected:
        raise ChatNotificationError(403, "INVALID_CHAT_RELATIONSHIP")


def parse_admin_thread_suffix(thread_id: str) -> Optional[str]:
    """Return the phone suffix for admin-{phone}, or None if malformed."""
    raw = str(thread_id or "").strip()
    if not raw.lower().startswith("admin-"):
        return None
    suffix = raw[6:].strip()
    if not suffix or "/" in suffix or not re.fullmatch(r"\d+", suffix):
        return None
    return suffix


def validate_message_id(message_id: str) -> None:
    mid = str(message_id or "").strip()
    if not mid or "/" in mid or mid in {".", ".."} or len(mid) > 128:
        raise ChatNotificationError(422, "INVALID_MESSAGE_ID")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", mid):
        raise ChatNotificationError(422, "INVALID_MESSAGE_ID")


def has_eligible_customer_vendor_relationship(
    db: Session,
    *,
    user_a: str,
    user_b: str,
) -> bool:
    """
    True when an unordered pair matches Request.customerAppId / requestWonBy
    on at least one BID - CONFIRMED or REQUEST - CONFIRMED row.
    """
    a = str(user_a or "").strip()
    b = str(user_b or "").strip()
    if not a or not b or a == b:
        return False

    row = (
        db.query(Request.RID)
        .filter(
            Request.requestStatus.in_(_ALLOWED_CHAT_STATUSES),
            Request.requestWonBy.isnot(None),
            Request.requestWonBy != "",
            or_(
                and_(Request.customerAppId == a, Request.requestWonBy == b),
                and_(Request.customerAppId == b, Request.requestWonBy == a),
            ),
        )
        .first()
    )
    return row is not None


def _load_user(db: Session, user_app_id: str) -> Optional[User]:
    return db.query(User).filter(User.userAppId == user_app_id).first()


def _pair_bucket_key(sender: str, receiver: str) -> str:
    a, b = sorted([str(sender).strip(), str(receiver).strip()])
    return f"chat_notification:pair:{a}:{b}"


def _support_pair_bucket_key(sender: str, receiver: str) -> str:
    a = _hash_bucket_part(sender)
    b = _hash_bucket_part(receiver)
    return f"chat_notification:support_pair:{a}:{b}"


def _send_fcm(*, title: str, body: str, fcm_token: str) -> Dict[str, str]:
    try:
        result = send_notification_to_token(
            title=title,
            body=body,
            fcm_token=fcm_token,
            url=_CHAT_DEEP_LINK,
            sound_file="normal_notification",
        )
    except Exception as exc:
        raise ChatNotificationError(500, "CHAT_NOTIFICATION_FAILED") from exc

    if not result.get("success"):
        raise ChatNotificationError(500, "CHAT_NOTIFICATION_FAILED")

    return {"message": "NOTIFICATION_SENT"}


def _consume_message_idempotency(
    db: Session, *, thread_id: str, message_id: str
) -> Optional[Dict[str, str]]:
    event_limited = enforce_rate_limit(
        db,
        bucket_key=f"chat_notification:{thread_id}:{message_id}",
        max_hits=1,
        window_seconds=_MESSAGE_EVENT_WINDOW_SECONDS,
    )
    if event_limited is not None:
        return {"message": "ALREADY_HANDLED"}
    return None


def _finalize_recipient_and_send(
    db: Session,
    *,
    recipient: User,
    thread_id: str,
    message_id: str,
    title: str,
    body: str,
) -> Dict[str, str]:
    already = _consume_message_idempotency(
        db, thread_id=thread_id, message_id=message_id
    )
    if already is not None:
        return already

    if _is_tombstone_user_app_id(getattr(recipient, "userAppId", None)):
        return {"message": "NOTIFICATION_SKIPPED"}
    if bool(getattr(recipient, "lockApp", False)):
        return {"message": "NOTIFICATION_SKIPPED"}

    fcm_token = _clean_token(getattr(recipient, "fcmToken", None))
    if not fcm_token:
        return {"message": "NO_TOKEN"}

    return _send_fcm(title=title, body=body, fcm_token=fcm_token)


def _dispatch_peer_notification(
    db: Session,
    *,
    sender: User,
    sender_id: str,
    thread_id: str,
    message_id: str,
    message_sender: str,
    message_receiver: str,
    message_type: Any,
    message_text: Any,
) -> Dict[str, str]:
    validate_peer_thread_id(thread_id, message_sender, message_receiver)

    if not has_eligible_customer_vendor_relationship(
        db, user_a=message_sender, user_b=message_receiver
    ):
        raise ChatNotificationError(403, "INVALID_CHAT_RELATIONSHIP")

    recipient = _load_user(db, message_receiver)
    if recipient is None:
        raise ChatNotificationError(404, "RECIPIENT_NOT_FOUND")

    sender_limited = enforce_rate_limit(
        db,
        bucket_key=f"chat_notification:sender:{sender_id}",
        max_hits=_SENDER_MAX,
        window_seconds=_WINDOW_SECONDS,
    )
    if sender_limited is not None:
        raise ChatNotificationError(429, "CHAT_NOTIFICATION_RATE_LIMITED")

    pair_limited = enforce_rate_limit(
        db,
        bucket_key=_pair_bucket_key(message_sender, message_receiver),
        max_hits=_PAIR_MAX,
        window_seconds=_WINDOW_SECONDS,
    )
    if pair_limited is not None:
        raise ChatNotificationError(429, "CHAT_NOTIFICATION_RATE_LIMITED")

    sender_name = str(getattr(sender, "fullName", None) or "").strip() or "OpenBid User"
    title = build_notification_title(sender_name)
    body = build_notification_body(message_type=message_type, text=message_text)

    return _finalize_recipient_and_send(
        db,
        recipient=recipient,
        thread_id=thread_id,
        message_id=message_id,
        title=title,
        body=body,
    )


def _dispatch_support_notification(
    db: Session,
    *,
    sender_id: str,
    thread_id: str,
    message_id: str,
    message_sender: str,
    message_receiver: str,
    message_type: Any,
) -> Dict[str, str]:
    identity = resolve_support_identity(db)
    if not identity.available or not identity.support_user_app_id:
        raise ChatNotificationError(503, "SUPPORT_CONFIGURATION_INVALID")

    support_id = identity.support_user_app_id
    suffix = parse_admin_thread_suffix(thread_id)
    if suffix is None:
        raise ChatNotificationError(422, "INVALID_THREAD_FORMAT")

    # --- Direction classification (JWT + verified RTDB + current config) ---
    user_to_support = (
        message_sender == sender_id
        and sender_id != support_id
        and message_receiver == support_id
        and suffix == sender_id
        and thread_id == f"admin-{sender_id}"
    )
    support_to_user = (
        sender_id == support_id
        and message_sender == support_id
        and bool(message_receiver)
        and message_receiver != support_id
        and suffix == message_receiver
        and thread_id == f"admin-{message_receiver}"
    )

    if not user_to_support and not support_to_user:
        # Includes forged admin-{otherUser}, support self-chat, mismatched
        # receiver, normal user pretending to be support, stale config.
        raise ChatNotificationError(403, "INVALID_SUPPORT_CHAT")

    if user_to_support:
        # Sender already gated as live/unlocked above.
        if sender_id == support_id:
            raise ChatNotificationError(403, "INVALID_SUPPORT_CHAT")

        sender_limited = enforce_rate_limit(
            db,
            bucket_key=(
                f"chat_notification:support_user_sender:{_hash_bucket_part(sender_id)}"
            ),
            max_hits=_SUPPORT_USER_SENDER_MAX,
            window_seconds=_WINDOW_SECONDS,
        )
        if sender_limited is not None:
            raise ChatNotificationError(429, "CHAT_NOTIFICATION_RATE_LIMITED")

        pair_limited = enforce_rate_limit(
            db,
            bucket_key=_support_pair_bucket_key(sender_id, support_id),
            max_hits=_SUPPORT_USER_PAIR_MAX,
            window_seconds=_WINDOW_SECONDS,
        )
        if pair_limited is not None:
            raise ChatNotificationError(429, "CHAT_NOTIFICATION_RATE_LIMITED")

        recipient = _load_user(db, support_id)
        if recipient is None:
            raise ChatNotificationError(503, "SUPPORT_CONFIGURATION_INVALID")
        # Configured support locked/tombstoned → fail closed (identity invalid).
        if _is_tombstone_user_app_id(getattr(recipient, "userAppId", None)):
            raise ChatNotificationError(503, "SUPPORT_CONFIGURATION_INVALID")
        if bool(getattr(recipient, "lockApp", False)):
            raise ChatNotificationError(503, "SUPPORT_CONFIGURATION_INVALID")

        title = _SUPPORT_USER_TITLE
        body = build_support_notification_body(message_type=message_type)

        already = _consume_message_idempotency(
            db, thread_id=thread_id, message_id=message_id
        )
        if already is not None:
            return already

        fcm_token = _clean_token(getattr(recipient, "fcmToken", None))
        if not fcm_token:
            return {"message": "NO_TOKEN"}

        return _send_fcm(title=title, body=body, fcm_token=fcm_token)

    # support → user
    op_limited = enforce_rate_limit(
        db,
        bucket_key=(
            f"chat_notification:support_operator:{_hash_bucket_part(support_id)}"
        ),
        max_hits=_SUPPORT_OP_SENDER_MAX,
        window_seconds=_WINDOW_SECONDS,
    )
    if op_limited is not None:
        raise ChatNotificationError(429, "CHAT_NOTIFICATION_RATE_LIMITED")

    pair_limited = enforce_rate_limit(
        db,
        bucket_key=_support_pair_bucket_key(support_id, message_receiver),
        max_hits=_SUPPORT_OP_PAIR_MAX,
        window_seconds=_WINDOW_SECONDS,
    )
    if pair_limited is not None:
        raise ChatNotificationError(429, "CHAT_NOTIFICATION_RATE_LIMITED")

    recipient = _load_user(db, message_receiver)
    if recipient is None:
        raise ChatNotificationError(404, "RECIPIENT_NOT_FOUND")

    title = _SUPPORT_TO_USER_TITLE
    body = build_support_notification_body(message_type=message_type)

    return _finalize_recipient_and_send(
        db,
        recipient=recipient,
        thread_id=thread_id,
        message_id=message_id,
        title=title,
        body=body,
    )


def dispatch_chat_notification(
    db: Session,
    *,
    jwt_sub: str,
    thread_id: str,
    message_id: str,
) -> Dict[str, str]:
    """
    Authorize + idempotent FCM dispatch for one committed RTDB chat message.

    Classifies peer vs support threads after RTDB verification.
    Returns a safe ``{"message": ...}`` outcome dict.
    Raises ChatNotificationError for hard HTTP failures.
    """
    sender_id = str(jwt_sub or "").strip()
    thread_id = str(thread_id or "").strip()
    message_id = str(message_id or "").strip()

    validate_message_id(message_id)

    # --- Sender gates (before RTDB / idempotency) ---
    sender = _load_user(db, sender_id)
    if sender is None:
        raise ChatNotificationError(404, "SENDER_NOT_FOUND")
    if _is_tombstone_user_app_id(getattr(sender, "userAppId", None)):
        raise ChatNotificationError(403, "CHAT_NOTIFICATION_NOT_ALLOWED")
    if bool(getattr(sender, "lockApp", False)):
        raise ChatNotificationError(403, "CHAT_NOTIFICATION_NOT_ALLOWED")

    # --- RTDB verification ---
    try:
        payload = get_chat_message(thread_id=thread_id, message_id=message_id)
    except ChatDatabaseUnavailable as exc:
        raise ChatNotificationError(503, "CHAT_DATABASE_UNAVAILABLE") from exc
    except ChatMessageReadError as exc:
        raise ChatNotificationError(503, "CHAT_DATABASE_UNAVAILABLE") from exc

    if payload is None:
        raise ChatNotificationError(404, "MESSAGE_NOT_FOUND")
    if payload.get("__non_object__"):
        raise ChatNotificationError(422, "INVALID_MESSAGE_FORMAT")

    message_sender = str(payload.get("sender") or "").strip()
    message_receiver = str(payload.get("receiver") or "").strip()
    message_type = payload.get("type")
    message_text = payload.get("text")

    if not message_sender:
        raise ChatNotificationError(403, "MESSAGE_SENDER_MISMATCH")
    if message_sender != sender_id:
        raise ChatNotificationError(403, "MESSAGE_SENDER_MISMATCH")
    if not message_receiver:
        raise ChatNotificationError(422, "INVALID_MESSAGE_FORMAT")
    if message_sender == message_receiver:
        raise ChatNotificationError(403, "CHAT_NOTIFICATION_NOT_ALLOWED")

    # --- Thread classification ---
    if thread_id.lower().startswith("admin-"):
        return _dispatch_support_notification(
            db,
            sender_id=sender_id,
            thread_id=thread_id,
            message_id=message_id,
            message_sender=message_sender,
            message_receiver=message_receiver,
            message_type=message_type,
        )

    if re.fullmatch(r"\d+-\d+", thread_id):
        return _dispatch_peer_notification(
            db,
            sender=sender,
            sender_id=sender_id,
            thread_id=thread_id,
            message_id=message_id,
            message_sender=message_sender,
            message_receiver=message_receiver,
            message_type=message_type,
            message_text=message_text,
        )

    raise ChatNotificationError(422, "INVALID_THREAD_FORMAT")
