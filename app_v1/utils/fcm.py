import jwt
import httpx
import os
import json
from typing import Dict,Any
from datetime import datetime
from dotenv import load_dotenv


load_dotenv()


def base64url_encode(data : str)-> str:
    """Encode data in base64url format."""
    import base64
    return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")

def generate_jwt(service_account : Dict[str,Any]) -> str:
    """Generate JWT for Firebase authentication."""

    now = int(datetime.now().timestamp())
    header = {"alg":"RS256","typ":"JWT"}
    payload = {
        "iss" : service_account["client_email"],
        "scope" : "https://www.googleapis.com/auth/firebase.messaging",
        "aud" : service_account["token_uri"],
        "iat" : now,
        "exp" : now + 3600
    }

    return jwt.encode(payload,service_account["private_key"],algorithm="RS256",headers=header)

def get_access_token(jwt_token: str) -> Dict[str, Any]:
    url = "https://oauth2.googleapis.com/token"
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token
    }
    try:
        with httpx.Client() as client:
            response = client.post(url, data=payload, timeout=30.0)
        if response.status_code >= 400:
            return {
                "message": "ERROR_GETTING_TOKEN",
                "status": response.status_code,
                "body": response.text
            }
        return response.json()
    except httpx.HTTPError as e:
        return {"message": "ERROR_GETTING_TOKEN", "error": str(e)}
    

def send_notification(
        title:str,
        body:str,
        fcm_token:str,
        url:str,
        source:str = None,
        destination:str = None,
        travel_date:str = None,
        pickup_time:str = None,        
        type:str = "passengernotification",
        sound_file:str = "normal_notification"
) -> Dict[str,Any]:
    """
    Send FCM notification to a device.
    Args:
        title: Notification title
        body: Notification message
        fcm_token: Device FCM token
        url: Custom URL
        source: Trip source
        destination: Trip destination
        travel_date: Travel date (e.g., 2025-02-20)
        pickup_time: Pickup time (e.g., 10:00 AM)
        env: Environment ('test' or other)
        type: Notification type (default: passengernotification)
        sound_file: Sound file name (default: sound1.wav)
    Returns:
        Dict with message and details or error
    """

    if not fcm_token or fcm_token.strip() == "":
        return {"message": "ERROR_MISSING_FCMTOKEN"}
    
    print(f"{fcm_token}")
    
    #Load Service Account
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if not service_account_json:
        return {"message":"ERROR_MISSING_SERVICE_ACCOUNT"}
    try:
        service_account = json.loads(service_account_json)
    except json.JSONDecodeError:
        return {"message":"ERROR_INVALID_SERVICE_ACCOUNT"}
    

    #Generate JWT and access tokens
    jwt_token = generate_jwt(service_account)
    # print(f'{jwt_token}')
    token_response = get_access_token(jwt_token)
    # print(f'{token_response}')
    if "access_token" not in token_response:
        return token_response
    
    access_token = token_response["access_token"]
    fcm_url = f"https://fcm.googleapis.com/v1/projects/{service_account['project_id']}/messages:send"

    #Construct Payload
    common_data={
        "title":title,
        "body" : body,
        "url" : url,
        "type" : type,
        "source" : source,
        "destination" : destination,
        "travelDate" : travel_date,
        "pickupTime" : pickup_time
    }

    
    payload = {
        "message": {
            "token": fcm_token,
            "data": {
                **common_data,
                "sound": sound_file
            },
            "apns": {
                "payload": {
                    "aps":{
                        "content-available":1
                    }
                    # "aps": {
                    #     "alert": {
                    #         "title": title,
                    #         "body": body
                    #     },
                    #     "sound": f"{sound_file}.caf",
                    #     "category": "customCategory",
                    #     "custom_data": common_data
                    # }
                },
                "headers": {
                    "apns-priority": "10"
                }
            }
        }
    }
    

    #Send FCM request

    headers = {
        "Authorization" : f"Bearer {access_token}",
        "Content-Type" : "application/json"
    }

    try:
        with httpx.Client() as client:
            response = client.post(fcm_url, json=payload, headers=headers, timeout=30.0)
        if response.status_code >= 400:
            # SHOW the actual FCM error payload
            return {
                "message": "ERROR_SENDING_NOTIFICATION",
                "status": response.status_code,
                "body": response.text
            }
        return {"message": "NOTIFICATION_SENT", "details": response.json()}
    except httpx.HTTPError as e:
        print(str(e))
        return {
            "message" : "ERROR_SENDING_NOTIFICATION",
            "error" : f"HTTP"
        }
    

