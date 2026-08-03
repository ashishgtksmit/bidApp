# import jwt
# import httpx
# import os
# import json
# from typing import Dict,Any
# from datetime import datetime
# from dotenv import load_dotenv


# load_dotenv()


# def base64url_encode(data : str)-> str:
#     """Encode data in base64url format."""
#     import base64
#     return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")

# def generate_jwt(service_account : Dict[str,Any]) -> str:
#     """Generate JWT for Firebase authentication."""

#     now = int(datetime.now().timestamp())
#     header = {"alg":"RS256","typ":"JWT"}
#     payload = {
#         "iss" : service_account["client_email"],
#         "scope" : "https://www.googleapis.com/auth/firebase.messaging",
#         "aud" : service_account["token_uri"],
#         "iat" : now,
#         "exp" : now + 3600
#     }

#     return jwt.encode(payload,service_account["private_key"],algorithm="RS256",headers=header)

# def get_access_token(jwt_token: str) -> Dict[str, Any]:
#     url = "https://oauth2.googleapis.com/token"
#     payload = {
#         "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
#         "assertion": jwt_token
#     }
#     try:
#         with httpx.Client() as client:
#             response = client.post(url, data=payload, timeout=30.0)
#         if response.status_code >= 400:
#             return {
#                 "message": "ERROR_GETTING_TOKEN",
#                 "status": response.status_code,
#                 "body": response.text
#             }
#         return response.json()
#     except httpx.HTTPError as e:
#         return {"message": "ERROR_GETTING_TOKEN", "error": str(e)}
    

# def send_notification(
#         title:str,
#         body:str,
#         fcm_token:str,
#         url:str,
#         source:str = None,
#         destination:str = None,
#         travel_date:str = None,
#         pickup_time:str = None,        
#         type:str = "passengernotification",
#         sound_file:str = "normal_notification"
# ) -> Dict[str,Any]:
#     """
#     Send FCM notification to a device.
#     Args:
#         title: Notification title
#         body: Notification message
#         fcm_token: Device FCM token
#         url: Custom URL
#         source: Trip source
#         destination: Trip destination
#         travel_date: Travel date (e.g., 2025-02-20)
#         pickup_time: Pickup time (e.g., 10:00 AM)
#         env: Environment ('test' or other)
#         type: Notification type (default: passengernotification)
#         sound_file: Sound file name (default: sound1.wav)
#     Returns:
#         Dict with message and details or error
#     """

#     if not fcm_token or fcm_token.strip() == "":
#         return {"message": "ERROR_MISSING_FCMTOKEN"}
    
#     print(f"{fcm_token}")
    
#     #Load Service Account
#     service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
#     if not service_account_json:
#         return {"message":"ERROR_MISSING_SERVICE_ACCOUNT"}
#     try:
#         service_account = json.loads(service_account_json)
#     except json.JSONDecodeError:
#         return {"message":"ERROR_INVALID_SERVICE_ACCOUNT"}
    

#     #Generate JWT and access tokens
#     jwt_token = generate_jwt(service_account)
#     # print(f'{jwt_token}')
#     token_response = get_access_token(jwt_token)
#     # print(f'{token_response}')
#     if "access_token" not in token_response:
#         return token_response
    
#     access_token = token_response["access_token"]
#     fcm_url = f"https://fcm.googleapis.com/v1/projects/{service_account['project_id']}/messages:send"

#     #Construct Payload
#     common_data={
#         "title":title,
#         "body" : body,
#         "url" : url,
#         "type" : type,
#         "source" : source,
#         "destination" : destination,
#         "travelDate" : travel_date,
#         "pickupTime" : pickup_time
#     }

    
#     # payload = {
#     #     "message": {
#     #         "token": fcm_token,
#     #         "data": {
#     #             **common_data,
#     #             "sound": sound_file
#     #         },
#     #         "apns": {
#     #             "payload": {
#     #                 "aps":{
#     #                     "content-available":1
#     #                 }
#     #                 # "aps": {
#     #                 #     "alert": {
#     #                 #         "title": title,
#     #                 #         "body": body
#     #                 #     },
#     #                 #     "sound": f"{sound_file}.caf",
#     #                 #     "category": "customCategory",
#     #                 #     "custom_data": common_data
#     #                 # }
#     #             },
#     #             "headers": {
#     #                 "apns-priority": "10"
#     #             }
#     #         }
#     #     }
#     # }
#     payload = {
#         "message": {
#             "token": fcm_token,
#             "data": {
#                 **common_data,
#                 "sound": sound_file
#             },
#             "apns": {
#                 "payload": {
#                     "aps": {
#                         "alert": {
#                             "title": title,
#                             "body": body
#                         },
#                         "sound": f"{sound_file}.caf",
#                         "category": "customCategory",
#                         "content-available": 1,
#                         "custom_data": {
#                             **common_data
#                         }
#                     }
#                 },
#                 "headers": {
#                     "apns-priority": "10"
#                 }
#             }
#         }
#     }
    

#     #Send FCM request

#     headers = {
#         "Authorization" : f"Bearer {access_token}",
#         "Content-Type" : "application/json"
#     }

#     try:
#         with httpx.Client() as client:
#             response = client.post(fcm_url, json=payload, headers=headers, timeout=30.0)
#         if response.status_code >= 400:
#             # SHOW the actual FCM error payload
#             return {
#                 "message": "ERROR_SENDING_NOTIFICATION",
#                 "status": response.status_code,
#                 "body": response.text
#             }
#         return {"message": "NOTIFICATION_SENT", "details": response.json()}
#     except httpx.HTTPError as e:
#         print(str(e))
#         return {
#             "message" : "ERROR_SENDING_NOTIFICATION",
#             "error" : f"HTTP"
#         }
    

import json
import os
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import firebase_admin
from firebase_admin import credentials, messaging
import httpx
import jwt
from dotenv import load_dotenv

load_dotenv()


DEFAULT_NOTIFICATION_TYPE = "passengernotification"
DEFAULT_SOUND_FILE = "normal_notification"
DEFAULT_APNS_PRIORITY = "10"
TOPIC_ALL_USERS = "all_users"
TOPIC_ALL_VENDORS = "all_vendors"
TOPIC_ALL_DRIVERS = "all_drivers"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _load_service_account() -> Dict[str, Any]:
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        raise ValueError("ERROR_MISSING_SERVICE_ACCOUNT")

    try:
        service_account = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("ERROR_INVALID_SERVICE_ACCOUNT") from e

    required_keys = {"client_email", "private_key", "token_uri", "project_id"}
    missing = [key for key in required_keys if not service_account.get(key)]
    if missing:
        raise ValueError(f"ERROR_INCOMPLETE_SERVICE_ACCOUNT: missing {', '.join(missing)}")

    return service_account


def _generate_jwt(service_account: Dict[str, Any]) -> str:
    now = int(_now_utc().timestamp())
    payload = {
        "iss": service_account["client_email"],
        "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": service_account["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }
    headers = {"alg": "RS256", "typ": "JWT"}

    return jwt.encode(
        payload,
        service_account["private_key"],
        algorithm="RS256",
        headers=headers,
    )


def _get_access_token(jwt_token: str, token_uri: str) -> Dict[str, Any]:
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(token_uri, data=payload)
    except httpx.HTTPError as e:
        return {
            "success": False,
            "message": "ERROR_GETTING_TOKEN",
            "error": str(e),
        }

    if response.status_code >= 400:
        return {
            "success": False,
            "message": "ERROR_GETTING_TOKEN",
            "status": response.status_code,
            "body": response.text,
        }

    token_data = response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return {
            "success": False,
            "message": "ERROR_GETTING_TOKEN",
            "body": token_data,
        }

    return {
        "success": True,
        "message": "TOKEN_RECEIVED",
        "access_token": access_token,
        "raw": token_data,
    }


def _get_fcm_credentials() -> Dict[str, Any]:
    try:
        service_account = _load_service_account()
    except ValueError as e:
        return {
            "success": False,
            "message": str(e),
        }

    jwt_token = _generate_jwt(service_account)
    token_response = _get_access_token(jwt_token, service_account["token_uri"])

    if not token_response.get("success"):
        return token_response

    return {
        "success": True,
        "message": "FCM_CREDENTIALS_READY",
        "project_id": service_account["project_id"],
        "access_token": token_response["access_token"],
    }


def _normalize_notification_data(
    title: str,
    body: str,
    url: str,
    notification_type: str = DEFAULT_NOTIFICATION_TYPE,
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
) -> Dict[str, str]:
    return {
        "title": _safe_str(title),
        "body": _safe_str(body),
        "url": _safe_str(url),
        "type": _safe_str(notification_type, DEFAULT_NOTIFICATION_TYPE),
        "source": _safe_str(source),
        "destination": _safe_str(destination),
        "travelDate": _safe_str(travel_date),
        "pickupTime": _safe_str(pickup_time),
    }


def _build_fcm_message(
    *,
    title: str,
    body: str,
    url: str,
    target_token: Optional[str] = None,
    target_topic: Optional[str] = None,
    notification_type: str = DEFAULT_NOTIFICATION_TYPE,
    sound_file: str = DEFAULT_SOUND_FILE,
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
) -> Dict[str, Any]:
    if not target_token and not target_topic:
        raise ValueError("Either target_token or target_topic is required")

    common_data = _normalize_notification_data(
        title=title,
        body=body,
        url=url,
        notification_type=notification_type,
        source=source,
        destination=destination,
        travel_date=travel_date,
        pickup_time=pickup_time,
    )

    message: Dict[str, Any] = {
        "data": {
            **common_data,
            "sound": _safe_str(sound_file, DEFAULT_SOUND_FILE),
        },
        "apns": {
            "payload": {
                "aps": {
                    "alert": {
                        "title": _safe_str(title),
                        "body": _safe_str(body),
                    },
                    "sound": f"{_safe_str(sound_file, DEFAULT_SOUND_FILE)}.caf",
                    "category": "customCategory",
                    "content-available": 1,
                    "custom_data": {
                        **common_data
                    },
                }
            },
            "headers": {
                "apns-priority": DEFAULT_APNS_PRIORITY
            },
        },
        "android": {
            "priority": "HIGH",
        },
    }

    if target_token:
        message["token"] = target_token
    else:
        message["topic"] = target_topic

    return {"message": message}


def _post_fcm_message(
    *,
    project_id: str,
    access_token: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    fcm_url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(fcm_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return {
            "success": False,
            "message": "ERROR_SENDING_NOTIFICATION",
            "error": str(e),
        }

    try:
        response_body = response.json()
    except Exception:
        response_body = response.text

    if response.status_code >= 400:
        return {
            "success": False,
            "message": "ERROR_SENDING_NOTIFICATION",
            "status": response.status_code,
            "body": response_body,
        }

    provider_message_id = None
    if isinstance(response_body, dict):
        provider_message_id = response_body.get("name")

    return {
        "success": True,
        "message": "NOTIFICATION_SENT",
        "provider_message_id": provider_message_id,
        "details": response_body,
    }


def send_notification_to_token(
    *,
    title: str,
    body: str,
    fcm_token: str,
    url: str,
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
    notification_type: str = DEFAULT_NOTIFICATION_TYPE,
    sound_file: str = DEFAULT_SOUND_FILE,
) -> Dict[str, Any]:
    if not fcm_token or not fcm_token.strip():
        return {
            "success": False,
            "message": "ERROR_MISSING_FCMTOKEN",
        }

    credentials = _get_fcm_credentials()
    if not credentials.get("success"):
        return credentials

    try:
        payload = _build_fcm_message(
            title=title,
            body=body,
            url=url,
            target_token=fcm_token.strip(),
            notification_type=notification_type,
            sound_file=sound_file,
            source=source,
            destination=destination,
            travel_date=travel_date,
            pickup_time=pickup_time,
        )
    except ValueError as e:
        return {
            "success": False,
            "message": "ERROR_BUILDING_PAYLOAD",
            "error": str(e),
        }

    return _post_fcm_message(
        project_id=credentials["project_id"],
        access_token=credentials["access_token"],
        payload=payload,
    )


def send_notification_to_topic(
    *,
    title: str,
    body: str,
    topic: str,
    url: str,
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
    notification_type: str = DEFAULT_NOTIFICATION_TYPE,
    sound_file: str = DEFAULT_SOUND_FILE,
) -> Dict[str, Any]:
    topic = _safe_str(topic).strip()
    if not topic:
        return {
            "success": False,
            "message": "ERROR_MISSING_TOPIC",
        }

    credentials = _get_fcm_credentials()
    if not credentials.get("success"):
        return credentials

    try:
        payload = _build_fcm_message(
            title=title,
            body=body,
            url=url,
            target_topic=topic,
            notification_type=notification_type,
            sound_file=sound_file,
            source=source,
            destination=destination,
            travel_date=travel_date,
            pickup_time=pickup_time,
        )
    except ValueError as e:
        return {
            "success": False,
            "message": "ERROR_BUILDING_PAYLOAD",
            "error": str(e),
        }

    return _post_fcm_message(
        project_id=credentials["project_id"],
        access_token=credentials["access_token"],
        payload=payload,
    )


def send_notification(
    title: str,
    body: str,
    fcm_token: str,
    url: str,
    source: Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    pickup_time: Optional[str] = None,
    type: str = DEFAULT_NOTIFICATION_TYPE,
    sound_file: str = DEFAULT_SOUND_FILE,
) -> Dict[str, Any]:
    """
    Backward-compatible wrapper for existing code.
    """
    return send_notification_to_token(
        title=title,
        body=body,
        fcm_token=fcm_token,
        url=url,
        source=source,
        destination=destination,
        travel_date=travel_date,
        pickup_time=pickup_time,
        notification_type=type,
        sound_file=sound_file,
    )

def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _normalize_topic_name(topic: str) -> str:
    topic = _safe_str(topic).strip()
    if topic.startswith("/topics/"):
        topic = topic[len("/topics/"):]
    return topic


def _normalize_token_list(tokens: List[str]) -> List[str]:
    cleaned = []
    seen = set()

    for token in tokens:
        token = _safe_str(token).strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in {"null", "na", "none"}:
            continue
        if token not in seen:
            cleaned.append(token)
            seen.add(token)

    return cleaned


@lru_cache(maxsize=1)
def _get_firebase_admin_app():
    """
    Initialize Firebase Admin SDK once and reuse it.
    Uses the same FIREBASE_SERVICE_ACCOUNT env JSON already used elsewhere.

    When FIREBASE_DATABASE_URL is set, it is applied at initialize time so
    messaging and RTDB share one OpenBid Admin app (PR26).
    """
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        raise ValueError("ERROR_MISSING_SERVICE_ACCOUNT")

    try:
        service_account = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("ERROR_INVALID_SERVICE_ACCOUNT") from e

    required_keys = {"project_id", "private_key", "client_email"}
    missing = [key for key in required_keys if not service_account.get(key)]
    if missing:
        raise ValueError(f"ERROR_INCOMPLETE_SERVICE_ACCOUNT: missing {', '.join(missing)}")

    options = None
    database_url = (os.getenv("FIREBASE_DATABASE_URL") or "").strip()
    if database_url:
        options = {"databaseURL": database_url}

    try:
        return firebase_admin.get_app()
    except ValueError:
        cred = credentials.Certificate(service_account)
        return firebase_admin.initialize_app(cred, options)


def subscribe_tokens_to_topic(tokens: List[str], topic: str) -> Dict[str, Any]:
    """
    Subscribe one or more FCM registration tokens to a topic.
    """
    topic = _normalize_topic_name(topic)
    tokens = _normalize_token_list(tokens)

    if not topic:
        return {
            "success": False,
            "message": "ERROR_MISSING_TOPIC",
            "successCount": 0,
            "failureCount": 0,
            "errors": [],
        }

    if not tokens:
        return {
            "success": False,
            "message": "ERROR_NO_VALID_TOKENS",
            "successCount": 0,
            "failureCount": 0,
            "errors": [],
        }

    try:
        _get_firebase_admin_app()
        response = messaging.subscribe_to_topic(tokens, topic)

        errors = []
        if getattr(response, "errors", None):
            for idx, err in enumerate(response.errors):
                errors.append(
                    {
                        "index": getattr(err, "index", idx),
                        "reason": str(getattr(err, "reason", "UNKNOWN_ERROR")),
                    }
                )

        return {
            "success": response.failure_count == 0,
            "message": "TOPIC_SUBSCRIBE_DONE",
            "topic": topic,
            "successCount": response.success_count,
            "failureCount": response.failure_count,
            "errors": errors,
        }

    except Exception as e:
        return {
            "success": False,
            "message": "ERROR_SUBSCRIBING_TOPIC",
            "topic": topic,
            "successCount": 0,
            "failureCount": len(tokens),
            "errors": [{"reason": str(e)}],
        }


def unsubscribe_tokens_from_topic(tokens: List[str], topic: str) -> Dict[str, Any]:
    """
    Unsubscribe one or more FCM registration tokens from a topic.
    """
    topic = _normalize_topic_name(topic)
    tokens = _normalize_token_list(tokens)

    if not topic:
        return {
            "success": False,
            "message": "ERROR_MISSING_TOPIC",
            "successCount": 0,
            "failureCount": 0,
            "errors": [],
        }

    if not tokens:
        return {
            "success": False,
            "message": "ERROR_NO_VALID_TOKENS",
            "successCount": 0,
            "failureCount": 0,
            "errors": [],
        }

    try:
        _get_firebase_admin_app()
        response = messaging.unsubscribe_from_topic(tokens, topic)

        errors = []
        if getattr(response, "errors", None):
            for idx, err in enumerate(response.errors):
                errors.append(
                    {
                        "index": getattr(err, "index", idx),
                        "reason": str(getattr(err, "reason", "UNKNOWN_ERROR")),
                    }
                )

        return {
            "success": response.failure_count == 0,
            "message": "TOPIC_UNSUBSCRIBE_DONE",
            "topic": topic,
            "successCount": response.success_count,
            "failureCount": response.failure_count,
            "errors": errors,
        }

    except Exception as e:
        return {
            "success": False,
            "message": "ERROR_UNSUBSCRIBING_TOPIC",
            "topic": topic,
            "successCount": 0,
            "failureCount": len(tokens),
            "errors": [{"reason": str(e)}],
        }


def subscribe_token_to_topics(token: str, topics: List[str]) -> Dict[str, Any]:
    """
    Convenience wrapper for one token to many topics.
    """
    token = _safe_str(token).strip()
    topics = [_normalize_topic_name(t) for t in topics if _normalize_topic_name(t)]

    if not token:
        return {
            "success": False,
            "message": "ERROR_NO_VALID_TOKEN",
            "results": {},
        }

    results = {}
    overall_success = True

    for topic in topics:
        result = subscribe_tokens_to_topic([token], topic)
        results[topic] = result
        if not result.get("success"):
            overall_success = False

    return {
        "success": overall_success,
        "message": "TOPIC_MULTI_SUBSCRIBE_DONE",
        "results": results,
    }


def unsubscribe_token_from_topics(token: str, topics: List[str]) -> Dict[str, Any]:
    """
    Convenience wrapper for one token to many topics.
    """
    token = _safe_str(token).strip()
    topics = [_normalize_topic_name(t) for t in topics if _normalize_topic_name(t)]

    if not token:
        return {
            "success": False,
            "message": "ERROR_NO_VALID_TOKEN",
            "results": {},
        }

    results = {}
    overall_success = True

    for topic in topics:
        result = unsubscribe_tokens_from_topic([token], topic)
        results[topic] = result
        if not result.get("success"):
            overall_success = False

    return {
        "success": overall_success,
        "message": "TOPIC_MULTI_UNSUBSCRIBE_DONE",
        "results": results,
    }