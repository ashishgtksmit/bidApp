# from sqlalchemy.orm import Session
# from ..utils.common import EmailErrorResponse,FCMSend,FCMSendDrivers,SendNotificationResponse
# from sqlalchemy.exc import SQLAlchemyError
# from ..utils.fcm import send_notification
# from ..crud.user import get_all_active_vendors
# from ..models.user_table import User
# from ..database import SessionLocal
# import json


# def send_notification_to_all_vendors(        
#         title : str,
#         body : str,        
#         notification_type : str,
#         sound_file : str
# ):
#     db=SessionLocal()
#     try :
#         with db.begin():
#             vendors = get_all_active_vendors(db)
#             if not vendors:
#                 print("No vendors to notify")
#                 return {"message": "NO_VENDORS"}

#             sent, skipped = 0, 0

#             for v in vendors:
#                 vendor_id = v.userAppId
#                 fcm_token = v.fcmToken

#                 if not fcm_token or fcm_token.strip().lower() == "null":
#                     skipped += 1
#                     # Optional: log which vendor was skipped
#                     # print(f"Skip: vendorId={vendor_id} missing FCM token")
#                     continue

#                 try:
#                     result = send_notification(
#                         title=title,
#                         body=body,
#                         fcm_token=fcm_token,
#                         url="/placebids",
#                         # If your send_notification requires these, either pass them:
#                         # source="", destination="", travel_date="", pickup_time="",
#                         type=notification_type,
#                         sound_file=sound_file
#                     )
#                     # Optional: inspect result for success/failure
#                     sent += 1
#                 except Exception as e:
#                     # Log and continue
#                     print(f"FCM error for vendorId={vendor_id}: {e}")

#             return {"message": "DONE", "sent": sent, "skipped": skipped}

#     finally:
#         db.close()


# # def send_notification_to_selected_users(db: Session, send_data : FCMSendDrivers):
# #     """
# #     Send FCM notification to list of driver userAppIds.
# #     Fetches fcmToken from userTable and calls sendFCMNotification().
# #     """
# #     driver_ids = [id.strip().lower for id in send_data.driverIds if id.strip()]
# #     if not driver_ids:
# #         return SendNotificationResponse(
# #             status="failure",
# #             totalProcessed=0,
# #             totalSuccess=0,
# #             results={}
# #         )
# #     try:
# #         # Get drivers from DB
# #         drivers = db.query(User).filter(User.userAppId.in_(driver_ids)).all()
# #         driver_map = {d.userAppId.lower(): d for d in drivers}

# #         total_processed=0
# #         total_success=0
# #         results={}

# #         for driver_id in driver_ids:
# #             driver = driver_map.get(driver_id)
# #             if not driver:
# #                 results[driver_id] = {"sent":False,"reason":"Driver not found"}
# #             fcm_token = driver.fcmToken
# #             if not fcm_token or fcm_token.strip() in ["", "null", "NULL"]:
# #                 results[driver_id] = {"sent": False, "reason": "No FCM token"}
# #                 continue

# #             fcm_data = {
# #                 "title" : send_data.title,
# #                 "body" : send_data.body,
# #                 "fcmToken" : fcm_token,
# #                 "url" : send_data.url,
# #                 "type" : "drivernotification",
# #                 "soundFile": send_data.soundFile
# #             }

# #             response = send_notification(fcm_data)
# #             total_processed += 1

# #             # Check if success (FCM v1 returns "name" field)
# #             try:
# #                 resp_json = json.loads(response) if isinstance(response, str) else response
# #                 success = bool(resp_json.get("name"))
# #             except:
# #                 success = False

# #             if success:
# #                 total_success += 1

# #             results[driver_id] = {
# #                 "sent": success,
# #                 "response": resp_json if 'resp_json' in locals() else response
# #             }
            
# #             return SendNotificationResponse(
# #                 status="success",
# #                 totalProcessed=total_processed,
# #                 totalSuccess=total_success,
# #                 results=results
# #             )
# #     except SQLAlchemyError as e:
# #         db.rollback()


# def send_notification_to_user(db: Session, notification_data : FCMSend):
#     """
#     Send FCM notification to a user based on userAppId.
#     """
#     try: 
#         with db.begin():
#             user = db.query(User).filter(User.userAppId == notification_data.userAppId).first()
#             if not user:
#                 return EmailErrorResponse(message="USER_NOT_FOUND")
#             fcm_token = user.fcmToken
#             print(f"{fcm_token}")
#             if not fcm_token:
#                 return EmailErrorResponse(message="NO_TOKEN")
#             fcm_result = send_notification(
#                 title=notification_data.title,
#                 body=notification_data.body,
#                 fcm_token=fcm_token,
#                 url=notification_data.url,
#                 type=notification_data.type,
#                 source=notification_data.source,
#                 destination=notification_data.destination,
#                 travel_date=notification_data.travelDate,
#                 pickup_time=notification_data.pickupTime,
#                 sound_file=notification_data.soundFile
#             )
#             if fcm_result["message"] != "NOTIFICATION_SENT":
#                 return EmailErrorResponse(message=f"ERROR_SENDING_NOTIFICATION", error=fcm_result.get("error", "Unknown error"))
#             return EmailErrorResponse(message="NOTIFICATION_SENT")            
#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_SENT",error=str(e))


# def send_notification_to_selected_users(db: Session,data: FCMSendDrivers):

#     # Clean driver IDs
#     driver_ids = [id.strip().lower() for id in data.driverIds if id.strip()]
#     if not driver_ids:
#         return SendNotificationResponse(
#             status="failure",
#             totalProcessed=0,
#             totalSuccess=0,
#             results={}
#         )

#     # Get drivers from DB
#     drivers = db.query(User).filter(User.userAppId.in_(driver_ids)).all()
#     driver_map = {d.userAppId.lower(): d for d in drivers}

#     total_processed = 0
#     total_success = 0
#     results = {}

#     # Loop through each requested driver
#     for driver_id in driver_ids:
#         driver = driver_map.get(driver_id)

#         if not driver:
#             results[driver_id] = {"sent": False, "reason": "Driver not found"}
#             continue

#         fcm_token = driver.fcmToken
#         if not fcm_token or fcm_token in ["", "null", "NULL"]:
#             results[driver_id] = {"sent": False, "reason": "No FCM token"}
#             continue

       
#         # Send notification
#         fcm_result = send_notification(
#                 title=data.title,
#                 body=data.body,
#                 fcm_token=fcm_token,
#                 url=data.url,
#                 sound_file=data.soundFile
#             )
#         print(str(fcm_result))
        
#         total_processed += 1

#         # Check if success (FCM v1 returns "name" field)
#         try:
#             resp_json = json.loads(fcm_result) if isinstance(fcm_result, str) else fcm_result
#             success = bool(resp_json.get("name"))
#         except:
#             success = False

#         if success:
#             total_success += 1

#         results[driver_id] = {
#             "sent": success,
#             "response": resp_json if 'resp_json' in locals() else fcm_result
#         }

#     return SendNotificationResponse(
#         status="success",
#         totalProcessed=total_processed,
#         totalSuccess=total_success,
#         results=results
#     )


from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.user_table import User
from ..models.bid_details import BidDetail
from ..utils.common import EmailErrorResponse, FCMSend, FCMSendDrivers, SendNotificationResponse
from ..utils.fcm import (
    TOPIC_ALL_USERS,
    TOPIC_ALL_VENDORS,
    send_notification_to_token,
    send_notification_to_topic,
    send_notification
)


def _clean_token(token: Optional[str]) -> str:
    token = str(token or "").strip()
    if not token or token.lower() in {"null", "none", "na"}:
        return ""
    return token


def _normalize_userappids(user_ids: List[str]) -> List[str]:
    cleaned = []
    seen = set()

    for user_id in user_ids:
        value = str(user_id or "").strip().lower()
        if not value:
            continue
        if value not in seen:
            cleaned.append(value)
            seen.add(value)

    return cleaned


def _build_send_result(
    *,
    success: bool,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "success": success,
        "message": message,
    }
    if extra:
        payload.update(extra)
    return payload


def send_notification_to_user(db: Session, notification_data: FCMSend):
    """
    Send FCM notification to a single user identified by userAppId.
    """
    try:
        user = db.query(User).filter(User.userAppId == notification_data.userAppId).first()
        if not user:
            return EmailErrorResponse(message="USER_NOT_FOUND")

        fcm_token = _clean_token(user.fcmToken)
        if not fcm_token:
            return EmailErrorResponse(message="NO_TOKEN")

        result = send_notification_to_token(
            title=notification_data.title,
            body=notification_data.body,
            fcm_token=fcm_token,
            url=notification_data.url,
            notification_type=notification_data.type,
            source=notification_data.source,
            destination=notification_data.destination,
            travel_date=notification_data.travelDate,
            pickup_time=notification_data.pickupTime,
            sound_file=notification_data.soundFile,
        )

        if not result.get("success"):
            return EmailErrorResponse(
                message="ERROR_SENDING_NOTIFICATION",
                error=str(result)
            )

        return EmailErrorResponse(message="NOTIFICATION_SENT")

    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_SENT", error=str(e))
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_SENT", error=str(e))


def send_notification_to_selected_users(db: Session, data: FCMSendDrivers):
    """
    Send notification to selected users/vendors by userAppId.
    """
    driver_ids = _normalize_userappids(data.driverIds)
    if not driver_ids:
        return SendNotificationResponse(
            status="failure",
            totalProcessed=0,
            totalSuccess=0,
            results={}
        )

    users = db.query(User).filter(User.userAppId.in_(driver_ids)).all()
    user_map = {str(user.userAppId).strip().lower(): user for user in users}

    total_processed = 0
    total_success = 0
    results: Dict[str, Any] = {}

    for driver_id in driver_ids:
        user = user_map.get(driver_id)
        if not user:
            results[driver_id] = {"sent": False, "reason": "User not found"}
            continue

        fcm_token = _clean_token(user.fcmToken)
        if not fcm_token:
            results[driver_id] = {"sent": False, "reason": "No FCM token"}
            continue

        result = send_notification_to_token(
            title=data.title,
            body=data.body,
            fcm_token=fcm_token,
            url=data.url,
            notification_type="drivernotification",
            sound_file=data.soundFile,
        )

        total_processed += 1
        success = bool(result.get("success"))

        if success:
            total_success += 1

        results[driver_id] = {
            "sent": success,
            "response": result
        }

    return SendNotificationResponse(
        status="success",
        totalProcessed=total_processed,
        totalSuccess=total_success,
        results=results
    )


def send_notification_to_all_users(
    *,
    title: str,
    body: str,
    url: str,
    notification_type: str = "default",
    sound_file: str = "normal_notification",
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Broadcast to all users via Firebase topic.
    """
    return send_notification_to_topic(
        title=title,
        body=body,
        topic=TOPIC_ALL_USERS,
        url=url,
        notification_type=notification_type,
        sound_file=sound_file,
        source=source,
        destination=destination,
        travel_date=travel_date,
        pickup_time=pickup_time,
    )


def send_notification_to_all_vendors(
    *,
    title: str,
    body: str,
    url: str = "/placebids",
    notification_type: str = "drivernotification",
    sound_file: str = "normal_notification",
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Broadcast to all vendors via Firebase topic.
    In your business model, vendor = driver broadcast audience.
    """
    return send_notification_to_topic(
        title=title,
        body=body,
        topic=TOPIC_ALL_VENDORS,
        url=url,
        notification_type=notification_type,
        sound_file=sound_file,
        source=source,
        destination=destination,
        travel_date=travel_date,
        pickup_time=pickup_time,
    )


def send_marketing_notification_to_numbers(
    db: Session,
    *,
    title: str,
    body: str,
    user_app_ids: List[str],
    url: str = "",
    sound_file: str = "normal_notification",
    notification_type: str = "default",
) -> Dict[str, Any]:
    """
    Send a marketing notification to a selected list of userAppIds / phone-number-style IDs.
    """
    normalized_ids = _normalize_userappids(user_app_ids)

    if not normalized_ids:
        return {
            "status": "failed",
            "totalProcessed": 0,
            "totalSuccess": 0,
            "totalFailed": 0,
            "noTokenIds": [],
            "failedIds": [],
        }

    users = db.query(User).filter(User.userAppId.in_(normalized_ids)).all()
    user_map = {str(user.userAppId).strip().lower(): user for user in users}

    total_processed = 0
    total_success = 0
    no_token_ids: List[str] = []
    failed_ids: List[str] = []

    for user_id in normalized_ids:
        total_processed += 1

        user = user_map.get(user_id)
        if not user:
            failed_ids.append(user_id)
            continue

        fcm_token = _clean_token(user.fcmToken)
        if not fcm_token:
            no_token_ids.append(user_id)
            continue

        result = send_notification_to_token(
            title=title,
            body=body,
            fcm_token=fcm_token,
            url=url,
            notification_type=notification_type,
            sound_file=sound_file,
        )

        if result.get("success"):
            total_success += 1
        else:
            failed_ids.append(user_id)

    total_failed = total_processed - total_success

    status = (
        "success" if total_success > 0 and total_failed == 0
        else "partial" if total_success > 0
        else "failed"
    )

    return {
        "status": status,
        "totalProcessed": total_processed,
        "totalSuccess": total_success,
        "totalFailed": total_failed,
        "noTokenIds": no_token_ids,
        "failedIds": failed_ids,
    }


def send_marketing_notification_to_all_users(
    *,
    title: str,
    body: str,
    url: str = "",
    sound_file: str = "normal_notification",
    notification_type: str = "default",
) -> Dict[str, Any]:
    """
    Marketing broadcast to all users via topic.
    """
    result = send_notification_to_all_users(
        title=title,
        body=body,
        url=url,
        notification_type=notification_type,
        sound_file=sound_file,
    )

    return {
        "status": "success" if result.get("success") else "failed",
        "totalProcessed": 1,
        "totalSuccess": 1 if result.get("success") else 0,
        "response": result,
    }


def send_notification_to_all_drivers(
    *,
    title: str,
    body: str,
    url: str = "",
    sound_file: str = "normal_notification",
    source: str = "",
    destination: str = "",
    travel_date: str = "",
    pickup_time: str = "",
) -> Dict[str, Any]:
    """
    In this project, vendor = driver audience.
    So this uses the vendor topic with drivernotification type.
    """
    result = send_notification_to_all_vendors(
        title=title,
        body=body,
        url=url,
        notification_type="drivernotification",
        sound_file=sound_file,
        source=source,
        destination=destination,
        travel_date=travel_date,
        pickup_time=pickup_time,
    )

    return {
        "status": "success" if result.get("success") else "failed",
        "totalProcessed": 1,
        "totalSuccess": 1 if result.get("success") else 0,
        "response": result,
    }


def notify_driver_assigned_to_customer(
    db: Session,
    *,
    customer_user_app_id: str,
    request_id: int,
    driver_name: Optional[str] = None,
    driver_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Reusable helper for request flow:
    notify customer that a driver has been assigned.
    """
    user = db.query(User).filter(User.userAppId == customer_user_app_id).first()
    if not user:
        return _build_send_result(success=False, message="USER_NOT_FOUND")

    fcm_token = _clean_token(user.fcmToken)
    if not fcm_token:
        return _build_send_result(success=False, message="NO_TOKEN")

    if driver_name or driver_number:
        who = driver_name or "your driver"
        num = f" ({driver_number})" if driver_number else ""
        body = f"{who}{num} has been assigned to your request #{request_id}."
    else:
        body = f"A driver has been assigned to your request #{request_id}."

    result = send_notification_to_token(
        title="Driver Assigned",
        body=body,
        fcm_token=fcm_token,
        url="/mytrips",
        notification_type="passengernotification",
        sound_file="alarm_notification",
    )

    return result


def notify_vendors_for_request(vendor_ids: List[str], create_data, db: Session) -> None:
    """
    Notify selected vendors about a newly created request.
    Intended for background task use.
    """
    try:
        if not vendor_ids:
            return

        notification_data = FCMSendDrivers(
            title="🚖 New Cab Request Alert! 🚖",
            body="A customer has just created a new cab request! 🏁💨\nSubmit your bid now and secure the ride.",
            url="///Place Bids",
            soundFile="normal_notification",
            driverIds=vendor_ids,
            source=create_data.fromLocation or "",
            destination=create_data.toLocation or "",
            travelDate=str(create_data.pickUpDate) if create_data.pickUpDate else "",
            pickupTime=str(create_data.pickUpTime) if create_data.pickUpTime else "",
        )

        send_notification_to_selected_users(db, notification_data)

    finally:
        db.close()


def notify_vendors_request_cancelled(db: Session, rid : int):
    """
    Notify all vendors who placed bids on this request
    """
    # Get vendor tokens
    rows = (
        db.query(User.fcmToken)
        .join(BidDetail, BidDetail.bidderID == User.userAppId)
        .filter(BidDetail.rID == rid)
        .all()
    )

    tokens = [
        t[0].strip()
        for t in rows
        if t[0] and t[0].strip().lower() not in ["", "null"]
    ]

    for token in tokens:
        try:
            send_notification(
                title="Bid Update: Request Cancelled",
                body="The request you had bid on has been cancelled by the user. 🚀",
                fcm_token=token,
                url="Bid Update: Request Cancelled",
                type="default",
                sound_file="normal_notification"
            )
        except Exception as e:
            print(f"[FCM ERROR] token={token} err={e}")