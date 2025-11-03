from sqlalchemy.orm import Session
from ..utils.common import EmailErrorResponse,FCMSend,FCMSendDrivers,SendNotificationResponse
from sqlalchemy.exc import SQLAlchemyError
from ..utils.fcm import send_notification
from ..crud.user import get_all_active_vendors
from ..models.user_table import User
from ..database import SessionLocal
import json


def send_notification_to_all_vendors(        
        title : str,
        body : str,        
        notification_type : str,
        sound_file : str
):
    db=SessionLocal()
    try :
        with db.begin():
            vendors = get_all_active_vendors(db)
            if not vendors:
                print("No vendors to notify")
                return {"message": "NO_VENDORS"}

            sent, skipped = 0, 0

            for v in vendors:
                vendor_id = v.userAppId
                fcm_token = v.fcmToken

                if not fcm_token or fcm_token.strip().lower() == "null":
                    skipped += 1
                    # Optional: log which vendor was skipped
                    # print(f"Skip: vendorId={vendor_id} missing FCM token")
                    continue

                try:
                    result = send_notification(
                        title=title,
                        body=body,
                        fcm_token=fcm_token,
                        url="/placebids",
                        # If your send_notification requires these, either pass them:
                        # source="", destination="", travel_date="", pickup_time="",
                        type=notification_type,
                        sound_file=sound_file
                    )
                    # Optional: inspect result for success/failure
                    sent += 1
                except Exception as e:
                    # Log and continue
                    print(f"FCM error for vendorId={vendor_id}: {e}")

            return {"message": "DONE", "sent": sent, "skipped": skipped}

    finally:
        db.close()


# def send_notification_to_selected_users(db: Session, send_data : FCMSendDrivers):
#     """
#     Send FCM notification to list of driver userAppIds.
#     Fetches fcmToken from userTable and calls sendFCMNotification().
#     """
#     driver_ids = [id.strip().lower for id in send_data.driverIds if id.strip()]
#     if not driver_ids:
#         return SendNotificationResponse(
#             status="failure",
#             totalProcessed=0,
#             totalSuccess=0,
#             results={}
#         )
#     try:
#         # Get drivers from DB
#         drivers = db.query(User).filter(User.userAppId.in_(driver_ids)).all()
#         driver_map = {d.userAppId.lower(): d for d in drivers}

#         total_processed=0
#         total_success=0
#         results={}

#         for driver_id in driver_ids:
#             driver = driver_map.get(driver_id)
#             if not driver:
#                 results[driver_id] = {"sent":False,"reason":"Driver not found"}
#             fcm_token = driver.fcmToken
#             if not fcm_token or fcm_token.strip() in ["", "null", "NULL"]:
#                 results[driver_id] = {"sent": False, "reason": "No FCM token"}
#                 continue

#             fcm_data = {
#                 "title" : send_data.title,
#                 "body" : send_data.body,
#                 "fcmToken" : fcm_token,
#                 "url" : send_data.url,
#                 "type" : "drivernotification",
#                 "soundFile": send_data.soundFile
#             }

#             response = send_notification(fcm_data)
#             total_processed += 1

#             # Check if success (FCM v1 returns "name" field)
#             try:
#                 resp_json = json.loads(response) if isinstance(response, str) else response
#                 success = bool(resp_json.get("name"))
#             except:
#                 success = False

#             if success:
#                 total_success += 1

#             results[driver_id] = {
#                 "sent": success,
#                 "response": resp_json if 'resp_json' in locals() else response
#             }
            
#             return SendNotificationResponse(
#                 status="success",
#                 totalProcessed=total_processed,
#                 totalSuccess=total_success,
#                 results=results
#             )
#     except SQLAlchemyError as e:
#         db.rollback()


def send_notification_to_user(db: Session, notification_data : FCMSend):
    """
    Send FCM notification to a user based on userAppId.
    """
    try: 
        with db.begin():
            user = db.query(User).filter(User.userAppId == notification_data.userAppId).first()
            if not user:
                return EmailErrorResponse(message="USER_NOT_FOUND")
            fcm_token = user.fcmToken
            print(f"{fcm_token}")
            if not fcm_token:
                return EmailErrorResponse(message="NO_TOKEN")
            fcm_result = send_notification(
                title=notification_data.title,
                body=notification_data.body,
                fcm_token=fcm_token,
                url=notification_data.url,
                type=notification_data.type,
                source=notification_data.source,
                destination=notification_data.destination,
                travel_date=notification_data.travelDate,
                pickup_time=notification_data.pickupTime,
                sound_file=notification_data.soundFile
            )
            if fcm_result["message"] != "NOTIFICATION_SENT":
                return EmailErrorResponse(message=f"ERROR_SENDING_NOTIFICATION", error=fcm_result.get("error", "Unknown error"))
            return EmailErrorResponse(message="NOTIFICATION_SENT")            
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_SENT",error=str(e))


def send_notification_to_selected_users(db: Session,data: FCMSendDrivers):
    # Clean driver IDs
    driver_ids = [id.strip().lower() for id in data.driverIds if id.strip()]
    if not driver_ids:
        return SendNotificationResponse(
            status="failure",
            totalProcessed=0,
            totalSuccess=0,
            results={}
        )

    # Get drivers from DB
    drivers = db.query(User).filter(User.userAppId.in_(driver_ids)).all()
    driver_map = {d.userAppId.lower(): d for d in drivers}

    total_processed = 0
    total_success = 0
    results = {}

    # Loop through each requested driver
    for driver_id in driver_ids:
        driver = driver_map.get(driver_id)

        if not driver:
            results[driver_id] = {"sent": False, "reason": "Driver not found"}
            continue

        fcm_token = driver.fcmToken
        if not fcm_token or fcm_token in ["", "null", "NULL"]:
            results[driver_id] = {"sent": False, "reason": "No FCM token"}
            continue

       
        # Send notification
        fcm_result = send_notification(
                title=data.title,
                body=data.body,
                fcm_token=fcm_token,
                url=data.url,
                sound_file=data.soundFile
            )
        print(str(fcm_result))
        
        total_processed += 1

        # Check if success (FCM v1 returns "name" field)
        try:
            resp_json = json.loads(fcm_result) if isinstance(fcm_result, str) else fcm_result
            success = bool(resp_json.get("name"))
        except:
            success = False

        if success:
            total_success += 1

        results[driver_id] = {
            "sent": success,
            "response": resp_json if 'resp_json' in locals() else fcm_result
        }

    return SendNotificationResponse(
        status="success",
        totalProcessed=total_processed,
        totalSuccess=total_success,
        results=results
    )