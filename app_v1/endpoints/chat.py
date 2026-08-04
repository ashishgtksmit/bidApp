"""PR26/PR27/PR28 — purpose-specific chat endpoints (JWT mobile).

Not a generic notification utility — does not require X-OpenBid-Internal-Key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser, get_current_user
from ..crud.admin_number import resolve_support_identity
from ..database import get_db
from ..schemas.chat_media import (
    ChatMediaCleanupRequest,
    ChatMediaCleanupResponse,
    ChatMediaUploadRequest,
    ChatMediaUploadResponse,
)
from ..schemas.chat_notifications import (
    ChatNotificationRequest,
    ChatNotificationResponse,
)
from ..schemas.support_chat import SupportChatConfigResponse
from ..services.chat_media import (
    ChatMediaError,
    cleanup_chat_media,
    upload_chat_media,
)
from ..services.chat_notifications import (
    ChatNotificationError,
    dispatch_chat_notification,
)

router = APIRouter()


@router.get(
    "/chat/support/config",
    response_model=SupportChatConfigResponse,
    responses={
        401: {"description": "Missing or invalid JWT"},
        503: {"description": "Support configuration query failure"},
    },
)
def get_support_chat_config(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return the current typed support configuration for authenticated clients.

    Never returns FCM tokens, email, bank/KYC, or AdminNumber dumps.
    Zero or multiple AdminNumber rows → available=false (safe unavailable).
    """
    user_id = current_user.user_app_id
    # user_id proves auth; config is global for the environment.
    _ = user_id
    try:
        identity = resolve_support_identity(db)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SUPPORT_CONFIGURATION_INVALID",
        ) from None

    if not identity.available or not identity.support_user_app_id:
        return SupportChatConfigResponse(
            available=False,
            supportUserAppId=None,
            displayName="OpenBid Support",
            profileImageUrl=None,
        )

    return SupportChatConfigResponse(
        available=True,
        supportUserAppId=identity.support_user_app_id,
        displayName=identity.display_name or "OpenBid Support",
        profileImageUrl=identity.profile_image_url
        if identity.profile_image_url is not None
        else "",
    )


@router.post(
    "/chat/notifications",
    response_model=ChatNotificationResponse,
    responses={
        401: {"description": "Missing or invalid JWT"},
        403: {"description": "Not allowed / invalid relationship / sender mismatch"},
        404: {"description": "Message or user not found"},
        422: {"description": "Invalid request or message format"},
        429: {"description": "Rate limited"},
        500: {"description": "Notification provider failure"},
        503: {"description": "Chat database or support configuration unavailable"},
    },
)
def create_chat_notification(
    body: ChatNotificationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Dispatch a server-owned FCM chat notification after a committed RTDB write.

    Handles customer↔vendor peer threads and admin-{phone} support threads.
    Body: threadId + messageId only. Recipient token, title, body, and URL are
    derived on the server. Notification failure never deletes the RTDB message.
    """
    user_id = current_user.user_app_id
    try:
        outcome = dispatch_chat_notification(
            db,
            jwt_sub=user_id,
            thread_id=body.threadId,
            message_id=body.messageId,
        )
        return ChatNotificationResponse(message=outcome["message"])
    except ChatNotificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        # Never expose Firebase / provider exception text.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CHAT_NOTIFICATION_FAILED",
        ) from None


@router.post(
    "/chat/media",
    response_model=ChatMediaUploadResponse,
    responses={
        401: {"description": "Missing or invalid JWT"},
        403: {"description": "Not allowed / invalid relationship / locked sender"},
        409: {"description": "Same messageId with different content"},
        413: {"description": "Decoded image exceeds 2 MB"},
        415: {"description": "Unsupported media type"},
        422: {"description": "Invalid request, messageId, or image"},
        429: {"description": "Rate limited"},
        500: {"description": "Storage provider failure"},
    },
)
def upload_chat_media_endpoint(
    body: ChatMediaUploadRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upload one authorized chat PHOTO to server-controlled Azure chat-docs storage.

    Body: threadId + messageId + mediaType PHOTO + content (data-URI/base64).
    No client-controlled sender/receiver/path/container. RTDB write remains on
    the client after a successful upload. Not the legacy ``/uploadchatdoc`` route.
    """
    user_id = current_user.user_app_id
    try:
        outcome = upload_chat_media(
            db,
            jwt_sub=user_id,
            thread_id=body.threadId,
            message_id=body.messageId,
            media_type=body.mediaType,
            content=body.content,
            file_name=body.fileName,
            mime_type=body.mimeType,
        )
        return ChatMediaUploadResponse(**outcome)
    except ChatMediaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CHAT_MEDIA_UPLOAD_FAILED",
        ) from None


@router.delete(
    "/chat/media",
    response_model=ChatMediaCleanupResponse,
    responses={
        401: {"description": "Missing or invalid JWT"},
        403: {"description": "Not allowed for this thread/blob"},
        409: {"description": "RTDB message already committed"},
        422: {"description": "Invalid threadId or messageId"},
        500: {"description": "Cleanup provider failure"},
        503: {"description": "Chat database unavailable"},
    },
)
def cleanup_chat_media_endpoint(
    body: ChatMediaCleanupRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pre-message compensation cleanup only.

    Deletes the deterministic blob for threadId+messageId when no RTDB message
    exists at ``Chats/{threadId}/{messageId}``. Never accepts URL/path/container
    from the client. Not a user-facing chat deletion API.
    """
    user_id = current_user.user_app_id
    try:
        outcome = cleanup_chat_media(
            db,
            jwt_sub=user_id,
            thread_id=body.threadId,
            message_id=body.messageId,
        )
        return ChatMediaCleanupResponse(**outcome)
    except ChatMediaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CHAT_MEDIA_CLEANUP_FAILED",
        ) from None
