import httpx
import os
from typing import Dict, Any
from datetime import datetime
from dotenv import load_dotenv
import re
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..utils.common import ErrorResponse
import random
import string

load_dotenv()


def send_exotel_sms(to: str, body: str) -> Dict[str, Any]:
    api_key = os.getenv('EXOTEL_API_KEY')
    api_token = os.getenv('EXOTEL_API_TOKEN')
    subdomain = os.getenv('EXOTEL_SUBDOMAIN', 'api.exotel.com')
    sid = os.getenv('EXOTEL_SID')

    if not all([api_key, api_token, sid]):
        return {"message": "ERROR_MISSING_CREDENTIALS"}

    # Basic validations
    if not to or not to.strip():
        return {"message": "ERROR_MISSING_TO"}
    if not re.match(r'^\+\d{10,15}$', to):  # E.164: +<country><number>
        return {"message": "ERROR_INVALID_PHONE"}
    if not body or not body.strip():
        return {"message": "ERROR_MISSING_BODY"}
    if len(body) > 160:
        return {"message": "ERROR_BODY_TOO_LONG"}

    url = f"https://{subdomain}/v1/Accounts/{sid}/Sms/send"
    post_data = {
        "From": "WZRIDE",  # must be approved sender ID for your account
        "To": to,
        "Body": body,
    }

    try:
        with httpx.Client() as client:  # <-- fix: parentheses
            resp = client.post(
                url,
                data=post_data,
                auth=(api_key, api_token),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=30,
            )

        if 200 <= resp.status_code < 300:
            # response may be JSON; if not, still return text fallback
            try:
                return {"message": "SMS_SENT", "details": resp.json()}
            except ValueError:
                return {"message": "SMS_SENT", "details": resp.text}

        # Capture server error details robustly
        try:
            err_json = resp.json()
            err_msg = err_json.get("message") or err_json.get("error") or str(err_json)
        except ValueError:
            err_msg = resp.text or "Unknown Error"

        return {
            "message": "ERROR_SENDING_SMS",
            "status": resp.status_code,
            "error": f"HTTP {resp.status_code}: {err_msg}",
        }

    except httpx.HTTPError as e:
        return {"message": "ERROR_SENDING_SMS", "error": str(e)}
    

def send_otp_to_user(db: Session, user_app_id: str):
    if not user_app_id or not user_app_id.strip():
        return ErrorResponse(message="ERROR_MISSING_USERAPPID")

    # Ensure E.164 format before calling send_exotel_sms
    # Example: assume Indian numbers; if user_app_id is "7022359323", convert to "+917022359323"
    to = user_app_id if user_app_id.startswith('+') else f"+91{user_app_id}"

    try:
        # no need for a DB transaction if you aren't writing
        otp = ''.join(random.choices(string.digits, k=4))
        sms_body = (
            f"{otp} is your OTP for the transaction that you are performing in WIZZRIDE. This OTP will be valid for the next 5 mins. Do not share OTP for Security Reason."
            # f"{otp} is your OTP for Wizzride. It is valid for 5 mins. "
            # "Do not share this OTP."
        )

        sms_result = send_exotel_sms(to, sms_body)
        if sms_result.get("message") != "SMS_SENT":
            # surface either server error or validation message
            details = sms_result.get("error") or sms_result.get("message") or "Unknown error"
            return ErrorResponse(message=f"ERROR_SENDING_OTP: {details}")

        # TODO: store OTP (hash) + expiry in DB or cache if you plan to verify it later
        return ErrorResponse(message="OTP_SENT")

    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")