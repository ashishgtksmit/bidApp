# import smtplib
# from email.mime.text import MIMEText
# from email.mime.multipart import MIMEMultipart
# from email.mime.base import MIMEBase
# from email import encoders
# import os
# from dotenv import load_dotenv
# from pydantic import EmailStr
# from typing import Optional,Dict,Any

# load_dotenv()

# def send_email(
#         message : str,
#         subject : str,
#         from_address : str,
#         from_name : str,
#         to_address : str,
#         to_name : str,
#         cc_address : Optional[EmailStr] = None,
#         cc_name : Optional[str] = None,
#         bcc_address : Optional[str] = None,
#         bcc_name : Optional[str] = None,
#         attachement_path : Optional[str] = None
# )->Dict[str,Any]:
    
#     """
#     Send an email using Gmail SMTP with fallback mechanism.
#     Args:
#         message: HTML email body
#         subject: Email subject
#         from_address: Sender email
#         from_name: Sender name
#         to_address: Recipient email
#         to_name: Recipient name
#         cc_address: CC email (optional)
#         cc_name: CC name (optional)
#         bcc_address: BCC email (optional)
#         bcc_name: BCC name (optional)
#         attachment_path: File path for attachment (optional)
#     Returns:
#         Dict with message and optional error
#     """

#     if not to_address or to_address.strip() == "":
#         return {"message":"ERROR_MISSING_TOADDRESS"}
#     if not subject or subject.strip() == "":
#         return {"message":"ERROR_MISSING_SUBJECT"}
#     if not message or message.strip() == "":
#         return {"message":"ERROR_MISSING_MESSAGE"}
#     if attachement_path and (not os.path.exists(attachement_path) or os.path.getsize(attachement_path) > 10 *1024 *1024):
#         return {"message":"ERROR_INVALID_ATTACHMENT"}
    
#     # Email configuration from environment variables

#     smtp_config = {
#         "reservations@wizzride.com":{
#             "username" : os.getenv("SMTP_RESERVATIONS_USERNAME"),
#             "password" : os.getenv("SMTP_RESERVATIONS_PASSWORD")
#         },
#         "customersupport@wizzride.com":{
#             "username" : os.getenv("SMTP_CUSTOMERSUPPORT_USERNAME"),
#             "password" : os.getenv("SMTP_CUSTOMERSUPPORT_PASSWORD")
#         },
#     }

#     fallback_config = {
#         "username" : os.getenv("SMTP_FALLBACK_USERNAME", "ticketdetails@wizzride.com"),
#         "password" : os.getenv("SMTP_FALLBACK_PASSWORD")
#     }

#     if from_address not in smtp_config:
#         return {"message" : "ERROR_INVALID_FROMADDRESS"}
    
#     smtp_host = "smtp.gmail.com"
#     smtp_port = 587

#     def try_send_mail(config,from_addr,from_n):
#         #Create email message

#         msg = MIMEMultipart()
#         msg["From"] = f"{from_n} <{from_addr}>"
#         msg["To"] = f"{to_name} <{to_address}>"
#         msg["Subject"] = subject
#         if cc_address and cc_name:
#             msg["Cc"] = f"{cc_name} <{cc_address}>"
#         if bcc_address and bcc_name:
#             msg["Bcc"] = f"{bcc_address} <{bcc_name}>"
        
#         #Add HTML Body
#         msg.attach(MIMEText(message,"html"))

#         #Add attachment if present
#         if attachement_path:
#             try:
#                 with open(attachement_path,"rb") as attachement:
#                     part = MIMEBase("application","octet-stream")
#                     part.set_payload(attachement.read())
#                 encoders.encode_base64(part)
#                 part.add_header(
#                     "Content-Disposition",
#                     f"attachment; filename={os.path.basename(attachement_path)}"
#                 )
#                 msg.attach(part)
#             except Exception as e : 
#                 return {"message" : "ERROR_INVALID_ATTACHMENT", "error": str(e)}
            
#         #Send Mail

#         try:
#             with smtplib.SMTP(smtp_host,smtp_port,timeout = 30) as server:
#                 server.starttls()
#                 server.login(config["username"], config["password"])
#                 server.send_message(msg)
#             return {"message":"SENT"}
#         except smtplib.SMTPException as e : 
#             return {"message" : "ERROR_SENDING_EMAIL", "error": str(e)}
        
#     # Try primary email
#     result = try_send_mail(smtp_config[from_address], from_address, from_name)
#     if result["message"] == "SENT":
#         return result

#     # Try fallback email
#     result = try_send_mail(fallback_config, fallback_config["username"], "Wizzride Team")
#     return result


import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import os
from dotenv import load_dotenv
from pydantic import EmailStr
from typing import Optional, Dict, Any
from datetime import datetime

load_dotenv()

def send_email(
    message: str,
    subject: str,
    from_address: str,
    from_name: str,
    to_address: str,
    to_name: str,
    cc_address: Optional[str] = None,
    cc_name: Optional[str] = None,
    bcc_address: Optional[str] = None,
    bcc_name: Optional[str] = None,
    attachment_path: Optional[str] = None,
    is_html: bool = True,
) -> Dict[str, Any]:
    """
    Send an email using Gmail SMTP with fallback mechanism.
    Adds better error detail, auto skip for invalid credentials,
    and structured logs.

    ``is_html`` defaults to True so existing server-owned HTML callers
    (PR16 / PR29 / car / driver) remain unchanged. Pass False for plain text.
    """

    # --- 1️⃣ Basic Validation ---
    if not to_address or not to_address.strip():
        return {"message": "ERROR_MISSING_TOADDRESS"}
    if not subject or not subject.strip():
        return {"message": "ERROR_MISSING_SUBJECT"}
    if not message or not message.strip():
        return {"message": "ERROR_MISSING_MESSAGE"}
    if attachment_path and (
        not os.path.exists(attachment_path)
        or os.path.getsize(attachment_path) > 10 * 1024 * 1024
    ):
        return {"message": "ERROR_INVALID_ATTACHMENT"}

    # --- 2️⃣ Load SMTP configs from environment ---
    smtp_config = {
        "reservations@wizzride.com": {
            "username": os.getenv("SMTP_RESERVATIONS_USERNAME"),
            "password": os.getenv("SMTP_RESERVATIONS_PASSWORD"),
        },
        "customersupport@wizzride.com": {
            "username": os.getenv("SMTP_CUSTOMERSUPPORT_USERNAME"),
            "password": os.getenv("SMTP_CUSTOMERSUPPORT_PASSWORD"),
        },
    }

    fallback_config = {
        "username": os.getenv("SMTP_FALLBACK_USERNAME", "ticketdetails@wizzride.com"),
        "password": os.getenv("SMTP_FALLBACK_PASSWORD"),
    }

    if from_address not in smtp_config:
        return {"message": "ERROR_INVALID_FROMADDRESS"}

    smtp_host = "smtp.gmail.com"
    smtp_port = 587
    mime_subtype = "html" if is_html else "plain"

    # --- 3️⃣ Inner mail sending function ---
    def try_send_mail(config, from_addr, from_n, *, used_fallback: bool = False):
        msg = MIMEMultipart()
        msg["From"] = f"{from_n} <{from_addr}>"
        msg["To"] = f"{to_name} <{to_address}>"
        msg["Subject"] = subject

        # CC/BCC: name optional so server-owned multi-address lists work.
        if cc_address:
            msg["Cc"] = (
                f"{cc_name} <{cc_address}>" if cc_name else str(cc_address)
            )
        if bcc_address:
            msg["Bcc"] = (
                f"{bcc_name} <{bcc_address}>" if bcc_name else str(bcc_address)
            )

        msg.attach(MIMEText(message, mime_subtype))

        # Attachment
        if attachment_path:
            try:
                with open(attachment_path, "rb") as attachment:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={os.path.basename(attachment_path)}"
                )
                msg.attach(part)
            except Exception:
                return {"message": "ERROR_INVALID_ATTACHMENT"}

        # --- 4️⃣ Send email ---
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls()
                server.login(config["username"], config["password"])
                server.send_message(msg)

            # Do not log recipient addresses or provider bodies (PII).
            print(f"{datetime.now()} ✅ Sent email via configured sender")
            return {"message": "SENT", "used_fallback": used_fallback}

        except smtplib.SMTPAuthenticationError as e:
            # Invalid app password or wrong credentials
            error_code = e.smtp_code
            print(f"{datetime.now()} ❌ Auth Error ({error_code})")
            return {
                "message": "ERROR_AUTH_FAILED",
                "error_code": error_code,
                "used_fallback": used_fallback,
            }

        except smtplib.SMTPRecipientsRefused:
            print(f"{datetime.now()} ❌ Invalid recipient")
            return {"message": "ERROR_INVALID_RECIPIENT", "used_fallback": used_fallback}

        except smtplib.SMTPConnectError:
            print(f"{datetime.now()} ❌ Connection error")
            return {"message": "ERROR_SMTP_CONNECTION", "used_fallback": used_fallback}

        except smtplib.SMTPException as e:
            print(f"{datetime.now()} ❌ SMTP general error: {type(e).__name__}")
            return {"message": "ERROR_SENDING_EMAIL", "used_fallback": used_fallback}

    # --- 5️⃣ Try primary ---
    result = try_send_mail(smtp_config[from_address], from_address, from_name)

    # Skip fallback if credentials are bad (no point retrying)
    if result["message"] == "SENT":
        return result
    if result["message"] == "ERROR_AUTH_FAILED":
        return result  # Don’t retry for invalid credentials

    # --- 6️⃣ Try fallback ---
    print(f"{datetime.now()} ⚠️ Retrying via fallback account...")
    return try_send_mail(
        fallback_config,
        fallback_config["username"],
        "Wizzride Team",
        used_fallback=True,
    )
