from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..utils.fcm import send_notification
from ..utils.email import send_email
from ..utils.common import ErrorResponse,EmailErrorResponse,FCMSend,EmailSend,FCMSendDrivers,SendNotificationResponse
from ..services.notifications import send_notification_to_user,send_notification_to_selected_users
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