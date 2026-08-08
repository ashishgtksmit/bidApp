from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from ..utils.fcm import send_notification
from ..utils.common import (
    ErrorResponse,
    EmailErrorResponse,
    FCMSend,
    FCMSendDrivers,
    SendNotificationResponse,
    InternalEmailSendRequest,
)
from ..utils.image import generate_vendor_document_sas,upload_support_docs_to_azure
from ..services.notifications import (send_notification_to_user,send_notification_to_selected_users,
                                      send_notification_to_all_drivers,send_marketing_notification_to_numbers,
                                      send_marketing_notification_to_all_users)
from ..services.internal_email import InternalEmailError, send_internal_email
from ..schemas.common_schema import (GenerateBlobSasRequest, GenerateBlobSasResponse, UploadSupportDocsErrorResponse,
                                     UploadSupportDocsRequest,UploadSupportDocsResponse, 
                                     SendNotificationToAllDriversRequest, SendNotificationToAllDriversResponse,
                                     SendMarketingNotificationToNumbersRequest, SendMarketingNotificationToNumbersResponse,
                                     SendMarketingNotificationToAllUsersRequest, SendMarketingNotificationToAllUsersResponse,
                                     SendNotificationToSelectedDriversRequest, SendNotificationToSelectedDriversResponse)
from ..database import get_db
from typing import Union
from ..auth.deps import AuthenticatedUser, get_current_user
from ..auth.internal import (
    require_internal_notification_access,
    require_internal_email_access,
)
from ..events.outbox import process_bound_flag_snapshot


router = APIRouter()

_NOTIFICATION_DISPATCH_FAILED = "NOTIFICATION_DISPATCH_FAILED"


def _raise_notification_dispatch_failed() -> None:
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=_NOTIFICATION_DISPATCH_FAILED,
    )


def _map_user_notify_result(result: EmailErrorResponse) -> EmailErrorResponse:
    message = getattr(result, "message", None) or ""
    if message == "NOTIFICATION_SENT":
        return EmailErrorResponse(message="NOTIFICATION_SENT")
    if message == "NO_TOKEN":
        return EmailErrorResponse(message="NO_TOKEN")
    if message == "USER_NOT_FOUND":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="USER_NOT_FOUND",
        )
    _raise_notification_dispatch_failed()


def _map_raw_token_notify_result(result: dict) -> EmailErrorResponse:
    if not isinstance(result, dict):
        _raise_notification_dispatch_failed()
    if result.get("success") or result.get("message") == "NOTIFICATION_SENT":
        return EmailErrorResponse(message="NOTIFICATION_SENT")
    if result.get("message") in {"ERROR_MISSING_FCMTOKEN", "NO_TOKEN"}:
        return EmailErrorResponse(message="NO_TOKEN")
    _raise_notification_dispatch_failed()


@router.post(
    "/sendemail",
    response_model=Union[ErrorResponse, EmailErrorResponse],
    include_in_schema=False,
)
def send_mail_to_user(
    email_data: InternalEmailSendRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_email_access),
):
    """
    Internal-only restricted email (PR31).

    Requires Bearer JWT + X-OpenBid-Internal-Key. Hidden from public OpenAPI.
    Plain text only; no CC/BCC/attachments; server-owned sender; recipient allow-list.
    """
    user_id = current_user.user_app_id
    try:
        return send_internal_email(db, jwt_sub=user_id, request=email_data)
    except InternalEmailError as err:
        raise HTTPException(status_code=err.status_code, detail=err.detail) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INTERNAL_EMAIL_FAILED",
        ) from None

@router.post("/sendfcmnotification",response_model=Union[ErrorResponse,EmailErrorResponse])
def send_notification_to_fcm_token(
    fcm_data : FCMSend,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_notification_access),
):
    """Internal/admin only — raw FCM token dispatch. Not for ordinary mobile JWTs."""
    user_id = current_user.user_app_id
    try:
        result = send_notification(
            title=fcm_data.title,
            body=fcm_data.body,
            fcm_token=fcm_data.fcmToken,
            url=fcm_data.url,
            source=fcm_data.source,
            destination=fcm_data.destination,
            travel_date=fcm_data.travelDate,
            pickup_time=fcm_data.pickupTime,
            type=fcm_data.type,
            sound_file=fcm_data.soundFile
        )
        return _map_raw_token_notify_result(result)
    except HTTPException:
        raise
    except Exception:
        _raise_notification_dispatch_failed()


@router.post("/notificationtodriver",response_model=Union[ErrorResponse,EmailErrorResponse])
def send_notification_to_userappid(
    notification_data : FCMSend,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_notification_access),
    db: Session = Depends(get_db),
):
    """Internal/admin only — notify by userAppId. Not for ordinary mobile JWTs."""
    user_id = current_user.user_app_id
    try:
        result = send_notification_to_user(db, notification_data)
        return _map_user_notify_result(result)
    except HTTPException:
        raise
    except Exception:
        _raise_notification_dispatch_failed()


@router.post("/readimageprivatepath",response_model=Union[GenerateBlobSasResponse,ErrorResponse])
def generate_azure_blob_sas_endpoint(data: GenerateBlobSasRequest,
                                        current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                                        db: Session = Depends(get_db)):
        user_id = current_user.user_app_id
        try:
            # Uses AZURE_ACCOUNT_NAME / AZURE_ACCOUNT_KEY / AZURE_CONTAINER from env.
            sas_url = generate_vendor_document_sas(blob_path=data.blobPath)
            return GenerateBlobSasResponse(message="SAS URL generated successfully", sasUrl=sas_url)
        except Exception as e:
            return ErrorResponse(message=str(e))
        
@router.post("/uploadchatdoc",response_model=Union[UploadSupportDocsResponse,UploadSupportDocsErrorResponse])
def upload_chat_support_docs(data: UploadSupportDocsRequest,
                            current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                            db: Session = Depends(get_db)):
    user_id = current_user.user_app_id
    raw = data.file
    if isinstance(raw, list):
        files = raw
    elif raw:
        files = [raw]
    else:
        files = []

    if not files:
         return UploadSupportDocsErrorResponse(status="error", message="No files provided for upload")
    
    result = upload_support_docs_to_azure(files)

    if result["status"] == "success":
        return UploadSupportDocsResponse(status="success", urls=result["urls"])
    
    return UploadSupportDocsErrorResponse(status="error", message=result.get("message", "Unknown error during upload"))



@router.post(
    "/sendnotificationtoalldrivers",
    response_model=Union[SendNotificationToAllDriversResponse, ErrorResponse]
)
def send_notification_to_all_drivers_api(
    data: SendNotificationToAllDriversRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_notification_access),
):
    """Internal/admin only — topic broadcast to all drivers/vendors."""
    user_id = current_user.user_app_id
    try:
        result = send_notification_to_all_drivers(
            title=data.title,
            body=data.message,
            url=data.url or "",
            sound_file=data.soundFile or "normal_notification",
            source=data.source or "",
            destination=data.destination or "",
            travel_date=data.travelDate or "",
            pickup_time=data.pickupTime or "",
        )

        return SendNotificationToAllDriversResponse(
            status=result.get("status", "failed"),
            totalProcessed=result.get("totalProcessed", 0),
            totalSuccess=result.get("totalSuccess", 0),
            response={"message": result.get("status", "failed")},
        )
    except HTTPException:
        raise
    except Exception:
        _raise_notification_dispatch_failed()
    
@router.post(
    "/sendmarketingnotificationtonumbers",
    response_model=Union[SendMarketingNotificationToNumbersResponse, ErrorResponse]
)
def send_marketing_notification_to_numbers_api(
    data: SendMarketingNotificationToNumbersRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_notification_access),
    db: Session = Depends(get_db),
):
    """Internal/admin only — marketing notify to selected userAppIds."""
    user_id = current_user.user_app_id
    try:
        ids = data.phoneNumber if isinstance(data.phoneNumber, list) else []

        result = send_marketing_notification_to_numbers(
            db,
            title=data.title,
            body=data.body,
            user_app_ids=ids,
            url=data.url or "",
            sound_file=data.soundFile or "normal_notification",
            notification_type=data.type or "default",
        )

        # Do not expose recipient identity lists to callers.
        return SendMarketingNotificationToNumbersResponse(
            status=result.get("status", "failed"),
            totalProcessed=result.get("totalProcessed", 0),
            totalSuccess=result.get("totalSuccess", 0),
            totalFailed=result.get("totalFailed", 0),
            noTokenIds=[],
            failedIds=[],
        )
    except HTTPException:
        raise
    except Exception:
        _raise_notification_dispatch_failed()
    
@router.post(
    "/sendmarketingnotificationtoallusers",
    response_model=Union[SendMarketingNotificationToAllUsersResponse, ErrorResponse]
)
def send_marketing_notification_to_all_users_api(
    data: SendMarketingNotificationToAllUsersRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_notification_access),
):
    """Internal/admin only — marketing topic broadcast to all users."""
    user_id = current_user.user_app_id
    try:
        result = send_marketing_notification_to_all_users(
            title=data.title,
            body=data.message,
            url=data.url or "",
            sound_file=data.soundFile or "normal_notification",
            notification_type=data.type or "default",
        )

        return SendMarketingNotificationToAllUsersResponse(
            status=result.get("status", "failed"),
            totalProcessed=result.get("totalProcessed", 0),
            totalSuccess=result.get("totalSuccess", 0),
            response={"message": result.get("status", "failed")},
        )
    except HTTPException:
        raise
    except Exception:
        _raise_notification_dispatch_failed()
    

@router.post(
    "/sendnotificationtoselecteddrivers",
    response_model=Union[SendNotificationToSelectedDriversResponse, ErrorResponse]
)
def send_notification_to_selected_drivers_api(
    data: SendNotificationToSelectedDriversRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _: None = Depends(require_internal_notification_access),
    db: Session = Depends(get_db),
):
    """
    Internal/admin only — notify selected driver userAppIds.

    Canonical single route (PR25 deduplicated the prior duplicate declaration).
    Mutations call ``send_notification_to_selected_users`` directly and do not
    require this HTTP surface.
    """
    user_id = current_user.user_app_id
    try:
        service_data = FCMSendDrivers(
            title=data.title,
            body=data.message,
            url=data.url or "",
            soundFile=data.soundFile or "normal_notification",
            driverIds=data.driverIds,
        )

        result = send_notification_to_selected_users(db, service_data)

        return SendNotificationToSelectedDriversResponse(
            status=result.status,
            totalProcessed=result.totalProcessed,
            totalSuccess=result.totalSuccess,
            results=result.results,
        )
    except HTTPException:
        raise
    except Exception:
        _raise_notification_dispatch_failed()


@router.get(
    "/domain-event-flag-snapshot",
    include_in_schema=False,
)
def domain_event_flag_snapshot(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Process-bound domain-event flag proof (PR40/PR41 ops).

    Requires authenticated JWT. Returns booleans / revision / instance hash
    only — no secrets, RID, or account identifiers.
    """
    _ = current_user  # auth gate only
    return process_bound_flag_snapshot(reason="http")
