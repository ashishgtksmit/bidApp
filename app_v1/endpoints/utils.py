from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..utils.fcm import send_notification
from ..utils.email import send_email
from ..utils.common import ErrorResponse,EmailErrorResponse,FCMSend,EmailSend,FCMSendDrivers,SendNotificationResponse
from ..utils.image import generate_azure_blob_sas,upload_support_docs_to_azure
from ..services.notifications import (send_notification_to_user,send_notification_to_selected_users,
                                      send_notification_to_all_drivers,send_marketing_notification_to_numbers,
                                      send_marketing_notification_to_all_users)
from ..schemas.common_schema import (GenerateBlobSasRequest, GenerateBlobSasResponse, UploadSupportDocsErrorResponse,
                                     UploadSupportDocsRequest,UploadSupportDocsResponse, 
                                     SendNotificationToAllDriversRequest, SendNotificationToAllDriversResponse,
                                     SendMarketingNotificationToNumbersRequest, SendMarketingNotificationToNumbersResponse,
                                     SendMarketingNotificationToAllUsersRequest, SendMarketingNotificationToAllUsersResponse,
                                     SendNotificationToSelectedDriversRequest, SendNotificationToSelectedDriversResponse)
from ..database import get_db
from typing import Union
from ..auth.deps import get_current_user_id


router = APIRouter()

@router.post("/sendemail",response_model=Union[ErrorResponse,EmailErrorResponse])
def send_mail_to_user(email_data:EmailSend, db : Session = Depends(get_db),
                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                      ):
    return send_email(
        message=email_data.message,
        subject=email_data.subject,
        from_address=email_data.from_address,
        from_name=email_data.from_name,
        to_address=email_data.to_address,
        to_name=email_data.to_name,
        cc_address=email_data.cc_address,
        cc_name=email_data.cc_name,
        bcc_address=email_data.bcc_address,
        bcc_name=email_data.bcc_name,
        attachment_path=email_data.attachment_path
        )

@router.post("/sendfcmnotification",response_model=Union[ErrorResponse,EmailErrorResponse])
def send_notification_to_fcm_token(fcm_data : FCMSend, user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                   ):
    return send_notification(
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


@router.post("/notificationtodriver",response_model=Union[ErrorResponse,EmailErrorResponse])
def send_notification_to_userappid(notification_data : FCMSend, 
                                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                   db: Session = Depends(get_db)):
    return send_notification_to_user(db,notification_data)

@router.post("/sendnotificationtoselecteddrivers",response_model=SendNotificationResponse)
def send_notification_to_selected_(driver_data : FCMSendDrivers, 
                                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                   db: Session = Depends(get_db)):
    return send_notification_to_selected_users(db,driver_data)


@router.post("/readimageprivatepath",response_model=Union[GenerateBlobSasResponse,ErrorResponse])
def generate_azure_blob_sas_endpoint(data: GenerateBlobSasRequest,
                                        user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                        db: Session = Depends(get_db)):
        try:
            sas_url = generate_azure_blob_sas(blob_path=data.blobPath)
            return GenerateBlobSasResponse(message="SAS URL generated successfully", sasUrl=sas_url)
        except Exception as e:
            return ErrorResponse(message=str(e))
        
@router.post("/uploadchatdoc",response_model=Union[UploadSupportDocsResponse,UploadSupportDocsErrorResponse])
def upload_chat_support_docs(data: UploadSupportDocsRequest,
                            user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                            db: Session = Depends(get_db)):
    files = data.file if isinstance(data.file, list) else []

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
    user_id: str = Depends(get_current_user_id),
):
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
            response=result.get("response", result),
        )
    except Exception as e:
        return ErrorResponse(message="ERROR", error=str(e))
    
@router.post(
    "/sendmarketingnotificationtonumbers",
    response_model=Union[SendMarketingNotificationToNumbersResponse, ErrorResponse]
)
def send_marketing_notification_to_numbers_api(
    data: SendMarketingNotificationToNumbersRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
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

        return SendMarketingNotificationToNumbersResponse(
            status=result.get("status", "failed"),
            totalProcessed=result.get("totalProcessed", 0),
            totalSuccess=result.get("totalSuccess", 0),
            totalFailed=result.get("totalFailed", 0),
            noTokenIds=result.get("noTokenIds", []),
            failedIds=result.get("failedIds", []),
        )
    except Exception as e:
        return ErrorResponse(message="ERROR", error=str(e))
    
@router.post(
    "/sendmarketingnotificationtoallusers",
    response_model=Union[SendMarketingNotificationToAllUsersResponse, ErrorResponse]
)
def send_marketing_notification_to_all_users_api(
    data: SendMarketingNotificationToAllUsersRequest,
    user_id: str = Depends(get_current_user_id),
):
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
            response=result.get("response"),
        )
    except Exception as e:
        return ErrorResponse(message="ERROR", error=str(e))
    

@router.post(
    "/sendnotificationtoselecteddrivers",
    response_model=Union[SendNotificationToSelectedDriversResponse, ErrorResponse]
)
def send_notification_to_selected_drivers_api(
    data: SendNotificationToSelectedDriversRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
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
    except Exception as e:
        return ErrorResponse(message="ERROR", error=str(e))