from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
import urllib
from ..utils.common import EmailErrorResponse,ErrorResponse
from ..models.driver_details import DriverDetail
from ..models.user_table import User
from ..schemas.driver_details import (UpdateDriverDetail,DeleteDriverDetail,CreateDriverDetail,DriverDetailResponse,
                                      UploadDriverDocumentRequest,UploadDriverDocumentResponse)
from ..utils.image import upload_image,azure_blob_upload,azure_blob_delete_by_url
from ..utils.email import send_email
import os
import re
import html
from datetime import datetime



# def update_driver_details(db : Session, driver_data : UpdateDriverDetail):
#     try: 
#         with db.begin():    
#             driver = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.DDID).first()
#             if not driver:
#                 return ErrorResponse(message="NOT_FOUND")
            
#             update_data = {
#                 "driverCity" : driver_data.driverCity.strip(),
#                 "driverNumber" : re.sub(r'\D','',driver_data.driverNumber),
#                 "tableTimestamp" : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#             }

#             # handle photo url

#             new_photo_url = None
#             if driver_data.driverPhotoImg and driver_data.driverPhotoImg.strip():
#                 safe_user = re.sub(r'[^A-Za-z0-9_\-]','',driver.userAppId)
#                 driver_folder = re.sub(r'[^A-Za-z0-9_\-\s]', '', driver.driverName or 'Driver').strip() or 'Driver'
#                 base_dir = f"vendorDocuments/{safe_user}/drivers/{driver_folder}"
#                 base_url = f"http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/{safe_user}/drivers/{urllib.parse.quote(driver_folder)}"
#                 file_stem = f"DriverPhoto_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
#                 upload_result = upload_image(driver_data.driverPhotoImg,base_dir,file_stem,base_url)
#                 if upload_result["message"] != "UPLOADED":
#                     return EmailErrorResponse(message="ERROR_SAVE_DRIVER_PHOTO", error=upload_result.get("error"))
#                 new_photo_url = update_data["driverPhoto"] = upload_result["url"]

#             # update driver
#             update = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.DDID).update(update_data)    
#             # db.commit()

#             if update == 0:
#                 return EmailErrorResponse(message="ERROR_UPDATE", error="Database update failed")
            
#             # Send email
#             html = f"""
#                 <html>
#                 <head>
#                     <title>Driver Updated - OpenBid</title>
#                     <style>
#                         body {{ font-family: Arial, sans-serif; color:#333; background:#f5f7fb; }}
#                         .wrap {{ max-width:760px; margin:24px auto; }}
#                         .box  {{ padding:18px 20px; border:1px solid #e6e9f0; border-radius:12px; background:#fff; }}
#                         h2    {{ margin:0 0 12px; color:#1f2d3d; }}
#                         .grid {{ display:grid; grid-template-columns:220px 1fr; gap:8px 14px; }}
#                         .k    {{ font-weight:600; color:#2c3e50; }}
#                         .sep  {{ height:1px; background:#eceff4; margin:14px 0; }}
#                         a     {{ color:#0b5ed7; text-decoration:none; }}
#                         .muted{{ color:#6b7280; }}
#                     </style>
#                 </head>
#                 <body>
#                     <div class="wrap">
#                         <div class="box">
#                             <h2>Driver Updated</h2>
#                             <div class="grid">
#                                 <div class="k">DRIVERID</div><div>{driver_data.DDID}</div>
#                                 <div class="k">User App ID</div><div>{driver.userAppId}</div>
#                                 <div class="k">Driver Name</div><div>{driver.driverName or 'N/A'}</div>
#                                 <div class="k">Old City</div><div>{driver.driverCity}</div>
#                                 <div class="k">New City</div><div>{driver_data.driverCity}</div>
#                                 <div class="k">Old Number</div><div>{driver.driverNumber}</div>
#                                 <div class="k">New Number</div><div>{update_data['driverNumber']}</div>
#                                 <div class="k">Photo</div><div>
#                                     {f'<a href="{new_photo_url}" target="_blank">{new_photo_url}</a>' if new_photo_url else '<span class="muted">Unchanged</span>'}
#                                 </div>
#                                 <div class="k">Updated On (IST)</div><div>{update_data['tableTimestamp']}</div>
#                             </div>
#                             <div class="sep"></div>
#                             <div class="muted">This is an automated message. Please do not reply.</div>
#                         </div>
#                     </div>
#                 </body>
#                 </html>
#             """
#             email_result = send_email(
#                 message=html,
#                 subject="Open Bid | Driver Updated",
#                 from_address="customersupport@wizzride.com",
#                 from_name = "WizzRide",
#                 to_address="openbidresourceteam@wizzride.com",
#                 to_name="OpenBid Resource Team"
#             )

#             if email_result["message"] != "SENT":
#                 pass
#                 # print(f"Email failed: {email_result.get('error')}")
#                 # Continue despite email failure, as in PHP
#             return ErrorResponse(message="UPDATED")
            
#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_FOUND",error=str(e))


# def update_driver_details(db : Session, driver_data : UpdateDriverDetail):
#     try:
#         # -----------------------------------------------------
#         # 1) BASIC VALIDATION
#         # -----------------------------------------------------
#         if not driver_data.DDID:
#             return ErrorResponse(message="ERROR", error="DRIVERID_REQUIRED")
#         if not driver_data.driverCity and not driver_data.driverNumber and not driver_data.driverPhotoImg:
#             return ErrorResponse(message="ERROR", error="NO_FIELDS_TO_UPDATE")
        
#         driver_city = driver_data.driverCity.strip() if driver_data.driverCity else None
#         driver_number = re.sub(r'\D', '', driver_data.driverNumber) if driver_data.driverNumber else None

#         if driver_city == "" or len(driver_number) < 10:
#             return ErrorResponse(message="ERROR_INVALID_FIELDS")
        
#         # -----------------------------------------------------
#         # 2) DB – FETCH EXISTING ROW
#         # -----------------------------------------------------
#         driver = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.DDID).first()
#         if not driver:
#             return ErrorResponse(message="NOT_FOUND")
        
#         user_app_id = driver.userAppId or ""
#         driver_name_raw = driver.driverName or "Driver"
#         old_city = driver.driverCity
#         old_number = driver.driverNumber
#         old_photo_url = driver.driverPhoto

#         # -----------------------------------------------------
#         # 3) PREPARE FOLDER / BLOB PATH
#         # -----------------------------------------------------

#         safe_user = re.sub(r"[^A-Za-z0-9_\-]", "", str(user_app_id or ""))
#         driver_folder = re.sub(r"[^A-Za-z0-9_\- ]", "", str(driver_name_raw or ""))
#         driver_folder = re.sub(r"\s+", " ", driver_folder).strip()
#         if driver_folder == "":
#             driver_folder = "Driver"

#         base_blob = f"{safe_user}/drivers/{driver_folder}/"
#         ts = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")

#         # -----------------------------------------------------
#         # 4) IF NEW PHOTO → upload to Azure (PUBLIC)
#         # -----------------------------------------------------

#         new_photo_url = None

#         if driver_data.driverPhotoImg and driver_data.driverPhotoImg.strip():
#             ok, url = azure_blob_upload(
#                 blob_name=f"{base_blob}DriverPhoto_{ts}",
#                 base64_data=driver_data.driverPhotoImg,
#                 make_public=True,
#             )
#             if not ok:
#                 return ErrorResponse(message="ERROR_SAVE_PHOTO", error=url or "ERROR_SAVE_PHOTO")
#             new_photo_url = url

#         # -----------------------------------------------------
#         # 5) UPDATE DB
#         # -----------------------------------------------------

#         table_timestamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

#         driver.driverCity = driver_city
#         driver.driverNumber = driver_number
#         driver.tableTimestamp = table_timestamp

#         if new_photo_url:
#             driver.driverPhoto = new_photo_url

#         db.commit()

#         # delete old blob if photo changed
#         if new_photo_url and old_photo_url:
#             azure_blob_delete_by_url(old_photo_url)

#         # -----------------------------------------------------
#         # 6) EMAIL SUMMARY
#         # -----------------------------------------------------

#         try:
#             photo_block = (
#                 f'<a href="{new_photo_url}" target="_blank">{new_photo_url}</a>'
#                 if new_photo_url else
#                 '<span class="muted">Unchanged</span>'
#             )

#             html = f"""
#             <html>
#             <head>
#                 <title>Driver Updated - OpenBid</title>
#                 <style>
#                     body {{ font-family: Arial, sans-serif; color:#333; background:#f5f7fb; }}
#                     .wrap {{ max-width:760px; margin:24px auto; }}
#                     .box  {{ padding:18px 20px; border:1px solid #e6e9f0; border-radius:12px; background:#fff; }}
#                     .grid {{ display:grid; grid-template-columns:220px 1fr; gap:8px 14px; }}
#                     .k    {{ font-weight:600; color:#2c3e50; }}
#                     .sep  {{ height:1px; background:#eceff4; margin:14px 0; }}
#                     a     {{ color:#0b5ed7; text-decoration:none; }}
#                     .muted{{ color:#6b7280; }}
#                 </style>
#             </head>
#             <body>
#             <div class="wrap">
#             <div class="box">
#                 <h2>Driver Updated</h2>

#                 <div class="grid">
#                     <div class="k">Driver ID</div><div>{driver_data.DDID}</div>
#                     <div class="k">User App ID</div><div>{user_app_id}</div>
#                     <div class="k">Driver Name</div><div>{driver_name_raw}</div>

#                     <div class="k">Old City</div><div>{old_city}</div>
#                     <div class="k">New City</div><div>{driver_city}</div>

#                     <div class="k">Old Number</div><div>{old_number}</div>
#                     <div class="k">New Number</div><div>{driver_number}</div>

#                     <div class="k">Photo</div><div>{photo_block}</div>

#                     <div class="k">Updated On</div><div>{table_timestamp}</div>
#                 </div>

#                 <div class="sep"></div>
#                 <div class="muted">This is an automated message. Please do not reply.</div>
#             </div>
#             </div>
#             </body>
#             </html>
#             """

#             send_email(
#                 message=html,
#                 subject="OpenBid | Driver Updated",
#                 from_address="ticketdetails@wizzride.com",
#                 from_name="WizzRide",
#                 to_address="openbidresourceteam@wizzride.com",
#                 to_name="OpenBid Resource Team",
#             )
#         except Exception:
#             pass

#         return ErrorResponse(message="UPDATE")

#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_FOUND", error=str(e))
#     except Exception as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_FOUND", error=str(e))

def update_driver_details(db: Session, driver_data):
    """
    PHP-parity behavior:
    - requires DRIVERID
    - requires driverCity and driverNumber
    - normalizes driverNumber to digits only
    - validates driverNumber length >= 10
    - optionally uploads new driver photo to Azure (public)
    - updates driver row
    - deletes old photo after successful DB commit
    - sends admin email with exact PHP-style HTML
    """

    def clean(v) -> str:
        return str(v or "").strip()

    tz = ZoneInfo("Asia/Kolkata")
    new_photo_url = None

    try:
        # -----------------------------------------------------
        # 1) BASIC VALIDATION (same as PHP)
        # -----------------------------------------------------
        if not getattr(driver_data, "DDID", None):
            return ErrorResponse(message="ERROR_MISSING_DRIVERID")

        if not hasattr(driver_data, "driverCity") or not hasattr(driver_data, "driverNumber"):
            return ErrorResponse(message="ERROR_INVALID_FIELDS")

        driver_id = clean(driver_data.DDID)
        driver_city = clean(driver_data.driverCity)
        driver_number = re.sub(r"\D+", "", clean(driver_data.driverNumber))

        if driver_city == "" or len(driver_number) < 10:
            return ErrorResponse(message="ERROR_INVALID_FIELDS")

        # -----------------------------------------------------
        # 2) DB – FETCH EXISTING ROW
        # -----------------------------------------------------
        driver = (
            db.query(DriverDetail)
            .filter(DriverDetail.DDID == driver_id)
            .first()
        )
        if not driver:
            return ErrorResponse(message="NOT_FOUND")

        # -----------------------------------------------------
        # 3) EXISTING VALUES
        # -----------------------------------------------------
        user_app_id = driver.userAppId or ""
        driver_name_raw = driver.driverName or ""
        old_city = driver.driverCity or ""
        old_number = driver.driverNumber or ""
        old_photo_url = driver.driverPhoto or None

        # Prepare folder structure exactly like PHP
        safe_user = re.sub(r"[^A-Za-z0-9_\-]", "", user_app_id)
        driver_folder = re.sub(r"[^A-Za-z0-9_\- ]", "", driver_name_raw)
        driver_folder = re.sub(r"\s+", " ", driver_folder).strip()
        if driver_folder == "":
            driver_folder = "Driver"

        base_blob = f"{safe_user}/drivers/{driver_folder}/"
        ts = datetime.now(tz).strftime("%Y%m%d_%H%M%S")

        # -----------------------------------------------------
        # 4) IF NEW PHOTO → upload to Azure (PUBLIC)
        # -----------------------------------------------------
        photo_input = getattr(driver_data, "driverPhotoImg", None)
        if photo_input and clean(photo_input) != "":
            ok, uploaded_url = azure_blob_upload(
                blob_name=f"{base_blob}DriverPhoto_{ts}",
                base64_data=photo_input,
                make_public=True,
            )
            if not ok:
                return ErrorResponse(message="ERROR_SAVE_PHOTO")
            new_photo_url = uploaded_url

        # -----------------------------------------------------
        # 5) UPDATE DB
        # -----------------------------------------------------
        table_timestamp = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        driver.driverCity = driver_city
        driver.driverNumber = driver_number
        driver.tableTimestamp = table_timestamp

        if new_photo_url:
            driver.driverPhoto = new_photo_url

        db.commit()

        # If photo changed, delete the old blob after successful commit
        if new_photo_url and old_photo_url:
            try:
                azure_blob_delete_by_url(old_photo_url)
            except Exception:
                pass

        # -----------------------------------------------------
        # 6) EMAIL SUMMARY (same HTML as PHP)
        # -----------------------------------------------------
        photo_block = (
            f'<a href="{new_photo_url}" target="_blank">{new_photo_url}</a>'
            if new_photo_url
            else '<span class="muted">Unchanged</span>'
        )

        html = f'''
        <html>
        <head>
            <title>Driver Updated - OpenBid</title>
            <style>
                body {{ font-family: Arial, sans-serif; color:#333; background:#f5f7fb; }}
                .wrap {{ max-width:760px; margin:24px auto; }}
                .box  {{ padding:18px 20px; border:1px solid #e6e9f0; border-radius:12px; background:#fff; }}
                .grid {{ display:grid; grid-template-columns:220px 1fr; gap:8px 14px; }}
                .k    {{ font-weight:600; color:#2c3e50; }}
                .sep  {{ height:1px; background:#eceff4; margin:14px 0; }}
                a     {{ color:#0b5ed7; text-decoration:none; }}
                .muted{{ color:#6b7280; }}
            </style>
        </head>
        <body>
        <div class="wrap">
        <div class="box">

            <h2>Driver Updated</h2>

            <div class="grid">
                <div class="k">Driver ID</div><div>{driver_id}</div>
                <div class="k">User App ID</div><div>{user_app_id}</div>
                <div class="k">Driver Name</div><div>{driver_name_raw}</div>

                <div class="k">Old City</div><div>{old_city}</div>
                <div class="k">New City</div><div>{driver_city}</div>

                <div class="k">Old Number</div><div>{old_number}</div>
                <div class="k">New Number</div><div>{driver_number}</div>

                <div class="k">Photo</div><div>{photo_block}</div>

                <div class="k">Updated On</div><div>{table_timestamp}</div>
            </div>

            <div class="sep"></div>
            <div class="muted">This is an automated message. Please do not reply.</div>

        </div>
        </div>
        </body>
        </html>
        '''

        try:
            send_email(
                message=html,
                subject="OpenBid | Driver Updated",
                from_address="ticketdetails@wizzride.com",
                from_name="WizzRide",
                to_address="openbidresourceteam@wizzride.com",
                to_name="OpenBid Resource Team",
            )
        except Exception:
            pass

        return ErrorResponse(message="UPDATE")

    except SQLAlchemyError as e:
        db.rollback()

        # safer cleanup: if new upload happened but DB failed, delete the new blob
        if new_photo_url:
            try:
                azure_blob_delete_by_url(new_photo_url)
            except Exception:
                pass

        return EmailErrorResponse(message="ERROR_FOUND", error=str(e))

    except Exception as e:
        db.rollback()

        if new_photo_url:
            try:
                azure_blob_delete_by_url(new_photo_url)
            except Exception:
                pass

        return EmailErrorResponse(message="ERROR_FOUND", error=str(e))

    
def delete_driver_by_id(db :Session, driver_data : DeleteDriverDetail):
    """
    Soft delete a driver by setting userAppId to '123456789' and send notification email.
    """
    try:
        with db.begin():
            driver = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.driverId).first()
            if not driver:
                return ErrorResponse(message="NOT_FOUND")
            
            # Store driver details for email 

            driver_details = {
                "DDID" : driver.DDID,
                "userAppId" : driver.userAppId,
                "driverName" : driver.driverName or "N/A",
                "driverNumber" : driver.driverNumber,
                "driverDOB" : driver.driverDOB.isoformat() if driver.driverDOB else "N/A",
                "driverGender" : driver.driverGender or "N/A",
                "driverCity" : driver.driverCity,
                "driverLicense" : driver.driverLicense,
                "driverDocument" : driver.driverDocument,
                "driverPhoto" : driver.driverPhoto,
                "tableTimestamp" : driver.tableTimestamp.strftime('%Y-%m-%d %H:%M:%S') if driver.tableTimestamp else "N/A"

            }

            # Soft Delete By updating userAppId

            update_data = {
                "userAppId" : "123456789",
                "tableTimestamp" : datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            updated = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.driverId).update(update_data)
            db.commit()

            if updated == 0:
                return EmailErrorResponse(message="ERROR_UPDATE",error="Database Update Failed")
            
            #Send Email 
            ist_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            license_html = f'<a href="{html.escape(driver_details['driverLicense'])}" target="_blank">{html.escape(driver_details["driverLicense"])}</a>' if driver_details["driverLicense"] else '<span class="muted">N/A</span>'
            document_html = f'<a href="{html.escape(driver_details['driverDocument'])}" target="_blank">{html.escape(driver_details["driverDocument"])}</a>' if driver_details["driverDocument"] else '<span class="muted">N/A</span>'
            photo_html = f'<a href="{html.escape(driver_details['driverPhoto'])}" target="_blank">{html.escape(driver_details["driverPhoto"])}</a>' if driver_details["driverPhoto"] else '<span class="muted">N/A</span>'
            reason_html = f'<div class="code">{html.escape(driver_data.reason.replace("\n", "<br>"))}</div>' if driver_data.reason else '<span class="muted">Not provided</span>'
            deleted_by_html = html.escape(driver_data.deletedBy) if driver_data.deletedBy else '<span class="muted">Not provided</span>'

            html_content = f"""
            <html>
            <head>
                <title>Driver Deleted - OpenBid</title>
                <style>
                    body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
                    .wrap {{ max-width: 760px; margin: 24px auto; }}
                    .box  {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
                    h2    {{ margin: 0 0 12px; color: #1f2d3d; }}
                    .grid {{ display: grid; grid-template-columns: 220px 1fr; gap: 8px 14px; }}
                    .k    {{ font-weight: 600; color: #2c3e50; }}
                    .sep  {{ height: 1px; background: #eceff4; margin: 14px 0; }}
                    .muted{{ color: #6b7280; }}
                    .code {{ background:#f3f4f6; padding: 8px 10px; border-radius:8px; }}
                </style>
            </head>
            <body>
                <div class="wrap">
                    <div class="box">
                        <h2>Driver Deleted (Soft)</h2>
                        <div class="grid">
                            <div class="k">DDID</div><div>{html.escape(str(driver_details["DDID"]))}</div>
                            <div class="k">Old User App ID</div><div>{html.escape(str(driver_details["userAppId"]))}</div>
                            <div class="k">New User App ID</div><div>123456789</div>
                            <div class="k">Driver Name</div><div>{html.escape(str(driver_details["driverName"]))}</div>
                            <div class="k">Driver Number</div><div>{html.escape(str(driver_details["driverNumber"]))}</div>
                            <div class="k">Driver DOB</div><div>{html.escape(str(driver_details["driverDOB"]))}</div>
                            <div class="k">Gender</div><div>{html.escape(str(driver_details["driverGender"]))}</div>
                            <div class="k">City</div><div>{html.escape(str(driver_details["driverCity"]))}</div>
                            <div class="k">License Image</div><div>{license_html}</div>
                            <div class="k">Document</div><div>{document_html}</div>
                            <div class="k">Photo</div><div>{photo_html}</div>                            
                            <div class="k">Created On (IST)</div><div>{html.escape(str(driver_details["tableTimestamp"]))}</div>
                            <div class="k">Deleted At (IST)</div><div>{html.escape(str(ist_now))}</div>
                            <div class="k">Deleted By</div><div>{deleted_by_html}</div>
                            <div class="k">Reason</div><div>{reason_html}</div>
                        </div>
                        <div class="sep"></div>
                        <div class="muted">This is an automated message. Please do not reply.</div>
                    </div>
                </div>
            </body>
            </html>
            """

            email_result = send_email(
                message=html_content,
                subject="OpenBid | Driver Deleted",
                from_address="customersupport@wizzride.com",
                from_name="Wizzride",
                to_address="openbidresourceteam@wizzride.com",
                to_name="OpenBid Resource Team"
            )

            if email_result["message"] != "SENT":
                pass

            return ErrorResponse(message="DELETED")
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_FOUND",error=str(e))
    
# def insert_driver(db: Session, driver_data : CreateDriverDetail):
#     """
#     Insert driver details into driverDetails table and save base64 images using upload_image.
#     """

#     #Configuration
#     BASE_LOCAL_DIR = os.path.join(os.path.dirname(__file__),'..','vendorDocuments')
#     BASE_PUBLIC_URL = 'http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/'
#     EMAIL_SUBJECT   = 'OpenBid | New Driver Added'
#     EMAIL_FROM      = 'customersupport@wizzride.com'
#     EMAIL_FROM_NAME  = 'WizzRide'
#     EMAIL_TO        = 'openbidresourceteam@wizzride.com'
#     EMAIL_TO_NAME    = 'OpenBid Resource Team'

#     #Check if Driver exists 
#     existing_driver = db.query(DriverDetail).filter(
#         DriverDetail.userAppId == driver_data.userAppId,
#         DriverDetail.driverNumber == driver_data.driverNumber
#     ).first()
#     if existing_driver:
#         return EmailErrorResponse(message="ERROR_ALREADY_EXISTS")
    
#     # File system work stays here
#     safe_user_id = re.sub(r'[^A-Za-z0-9_-]', '', driver_data.userAppId)
#     driver_folder = re.sub(r'[^A-Za-z0-9_-]', '', driver_data.driverName) or 'Driver'
#     driver_folder = ' '.join(driver_folder)

#     timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
#     driver_dir = os.path.join(BASE_LOCAL_DIR,safe_user_id,'drivers',driver_folder)
#     base_url = f"{BASE_PUBLIC_URL}{safe_user_id}/drivers/{driver_folder}"


#     license_result = upload_image(driver_data.driverLicenseImg,base_dir=driver_dir,file_stem=f"DriverDocument_{timestamp}",base_url=base_url)
#     if license_result["message"] != "UPLOADED":
#         return EmailErrorResponse(message="ERROR_INVALID_LICENSE_IMAGE",error=license_result.get("error"))
    
#     document_result = upload_image(driver_data.driverDocumentImg,base_dir=driver_dir,file_stem=f"DriverDocument_{timestamp}",base_url=base_url)
#     if document_result["message"] != "UPLOADED":
#         return EmailErrorResponse(message="ERROR_INVALID_DOCUMENT_IMAGE",error=document_result.get("error"))
    
#     photo_result = upload_image(driver_data.driverPhotoImg,base_dir=driver_dir,file_stem=f"DriverDocument_{timestamp}",base_url=base_url)
#     if photo_result["message"] != "UPLOADED":
#         return EmailErrorResponse(message="ERROR_INVALID_PHOTO_IMAGE",error=photo_result.get("error"))
    
#     license_url = license_result["url"]
#     document_url = document_result["url"]
#     photo_url = photo_result["url"]

#     try:
#         new_driver = DriverDetail(
#             userAppId=driver_data.userAppId,
#             driverName=driver_data.driverName,
#             driverNumber=driver_data.driverNumber,
#             driverDOB=driver_data.driverDOB,
#             driverGender=driver_data.driverGender,
#             driverCity=driver_data.driverCity,
#             driverLicense=license_url,
#             driverDocument=document_url,
#             driverPhoto=photo_url,
#             tableTimestamp=datetime.now()
#         )

#         db.add(new_driver)
#         db.commit()
        

#         html_content = f"""
#         <html>
#         <head>
#             <title>New Driver Added - OpenBid</title>
#             <style>
#                 body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
#                 .wrap {{ max-width: 760px; margin: 24px auto; }}
#                 .box {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
#                 h2 {{ margin: 0 0 12px; color: #1f2d3d; }}
#                 .grid {{ display: grid; grid-template-columns: 220px 1fr; gap: 8px 14px; }}
#                 .k {{ font-weight: 600; color: #2c3e50; }}
#                 .sep {{ height: 1px; background: #eceff4; margin: 14px 0; }}
#                 a {{ color: #0b5ed7; text-decoration: none; }}
#                 .muted {{ color: #6b7280; }}
#             </style>
#         </head>
#         <body>
#             <div class="wrap">
#                 <div class="box">
#                     <h2>New Driver Added</h2>
#                     <div class="grid">
#                         <div class="k">User App ID</div><div>{driver_data.userAppId}</div>
#                         <div class="k">Driver Name</div><div>{driver_data.driverName}</div>
#                         <div class="k">Driver Number</div><div>{driver_data.driverNumber}</div>
#                         <div class="k">Driver DOB</div><div>{driver_data.driverDOB}</div>
#                         <div class="k">Gender</div><div>{driver_data.driverGender}</div>
#                         <div class="k">City</div><div>{driver_data.driverCity}</div>
#                         <div class="k">License Image</div><div><a href="{license_url}" target="_blank">{license_url}</a></div>
#                         <div class="k">Document</div><div><a href="{document_url}" target="_blank">{document_url}</a></div>
#                         <div class="k">Photo</div><div><a href="{photo_url}" target="_blank">{photo_url}</a></div>
#                         <div class="k">Admin Approved</div><div>Pending</div>
#                         <div class="k">Added On (IST)</div><div>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
#                     </div>
#                     <div class="sep"></div>
#                     <div class="muted">This is an automated message. Please do not reply.</div>
#                 </div>
#             </div>
#         </body>
#         </html>
#         """

#         try:
#             email_result = send_email(
#                 message=html_content,
#                 subject=EMAIL_SUBJECT,
#                 from_address=EMAIL_FROM,
#                 from_name=EMAIL_FROM_NAME,
#                 to_address=EMAIL_TO,
#                 to_name=EMAIL_TO_NAME
#             )
#             print(email_result)
#         except Exception as e:
#             print(str(e))

#         return EmailErrorResponse(message="INSERTED")
#     except SQLAlchemyError as e: 
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_INSERT",error=str(e))
#     except ValueError as e : 
#         db.rollback()
#         return EmailErrorResponse(message="ERROR",error=str(e))


def insert_driver(db: Session, driver_data: CreateDriverDetail):

    EMAIL_SUBJECT = 'OpenBid | New Driver Added'
    EMAIL_FROM = 'ticketdetails@wizzride.com'
    EMAIL_FROM_NAME = 'WizzRide'
    EMAIL_TO = 'openbidresourceteam@wizzride.com'
    EMAIL_TO_NAME = 'OpenBid Resource Team'

    def clean(v):
        return str(v or "").strip()

    tz = ZoneInfo("Asia/Kolkata")

    # -----------------------------------------------------
    # 1) REQUIRED FIELDS (exact PHP)
    # -----------------------------------------------------
    required_fields = {
        'userAppId': driver_data.userAppId,
        'driverName': driver_data.driverName,
        'driverNumber': driver_data.driverNumber,
        'driverDOB': driver_data.driverDOB,
        'driverGender': driver_data.driverGender,
        'driverCity': driver_data.driverCity,
        'driverLicenseImg': driver_data.driverLicenseImg,
        'driverDocumentImg': driver_data.driverDocumentImg,
        'driverPhotoImg': driver_data.driverPhotoImg
    }

    for key, value in required_fields.items():
        if value is None or clean(value) == "":
            return EmailErrorResponse(message=f"ERROR_MISSING_{key.upper()}")

    # -----------------------------------------------------
    # 2) CLEAN INPUT (exact PHP logic)
    # -----------------------------------------------------
    user_app_id = re.sub(r'\s+', '', clean(driver_data.userAppId))
    safe_user = re.sub(r'[^A-Za-z0-9_\-]', '', user_app_id)

    driver_name_raw = clean(driver_data.driverName)

    driver_folder = re.sub(r'[^A-Za-z0-9_\- ]', '', driver_name_raw)
    driver_folder = re.sub(r'\s+', '_', driver_folder.strip())
    if driver_folder == "":
        driver_folder = "Driver"

    # Driver number cleanup
    driver_number = re.sub(r'\D+', '', clean(driver_data.driverNumber))
    if len(driver_number) < 10:
        return EmailErrorResponse(message="ERROR_INVALID_DRIVERNUMBER")

    # Gender normalization (exact PHP)
    g = clean(driver_data.driverGender).upper()
    if g in ['M', 'MALE']:
        driver_gender = "M"
    elif g in ['F', 'FEMALE']:
        driver_gender = "F"
    elif g in ['O', 'OTHER']:
        driver_gender = "O"
    else:
        return EmailErrorResponse(message="ERROR_INVALID_GENDER")

    # DOB parsing (same flexibility as PHP)
    dob_input = clean(driver_data.driverDOB)
    driver_dob = None

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            driver_dob = datetime.strptime(dob_input, fmt).strftime("%Y-%m-%d")
            break
        except:
            continue

    if not driver_dob:
        return EmailErrorResponse(message="ERROR_INVALID_DOB")

    driver_city = clean(driver_data.driverCity)

    table_timestamp = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    # -----------------------------------------------------
    # 3) AZURE PATH (exact PHP)
    # -----------------------------------------------------
    base_blob = f"{safe_user}/drivers/{driver_folder}/"

    new_blob_urls = []

    try:
        with db.begin():

            # Duplicate check (same as PHP)
            existing = db.query(DriverDetail).filter(
                DriverDetail.userAppId == user_app_id,
                DriverDetail.driverNumber == driver_number
            ).first()

            if existing:
                return EmailErrorResponse(message="ERROR_ALREADY_EXISTS")

            # -----------------------------------------------------
            # 4) AZURE UPLOAD (same naming strategy as PHP)
            # -----------------------------------------------------

            # License (PUBLIC)
            ok_lic, driver_license_url = azure_blob_upload(
                blob_name=f"{base_blob}DriverLicense_{driver_number}",
                base64_data=driver_data.driverLicenseImg,
                make_public=True
            )
            if not ok_lic:
                raise ValueError("ERROR_SAVE_LICENSE")
            new_blob_urls.append(driver_license_url)

            # Document (PRIVATE)
            ok_doc, driver_document_url = azure_blob_upload(
                blob_name=f"{base_blob}DriverDocument_{driver_number}",
                base64_data=driver_data.driverDocumentImg,
                make_public=False
            )
            if not ok_doc:
                raise ValueError("ERROR_SAVE_DOCUMENT")
            new_blob_urls.append(driver_document_url)

            # Photo (PUBLIC)
            ok_photo, driver_photo_url = azure_blob_upload(
                blob_name=f"{base_blob}DriverPhoto_{driver_number}",
                base64_data=driver_data.driverPhotoImg,
                make_public=True
            )
            if not ok_photo:
                raise ValueError("ERROR_SAVE_PHOTO")
            new_blob_urls.append(driver_photo_url)

            # -----------------------------------------------------
            # 5) INSERT DB
            # -----------------------------------------------------
            new_driver = DriverDetail(
                userAppId=user_app_id,
                driverName=driver_name_raw,
                driverNumber=driver_number,
                driverDOB=driver_dob,
                driverGender=driver_gender,
                driverCity=driver_city,
                driverLicense=driver_license_url,
                driverDocument=driver_document_url,
                driverPhoto=driver_photo_url,
                tableTimestamp=table_timestamp
            )

            db.add(new_driver)

        # -----------------------------------------------------
        # 6) EMAIL (EXACT PHP HTML)
        # -----------------------------------------------------
        html = f'''
        <html>
        <head>
            <title>New Driver Added - OpenBid</title>
            <style>
                body {{ font-family: Arial, sans-serif; color: #333; }}
                .box  {{ padding: 18px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; max-width:760px; margin:auto; }}
                .row  {{ margin-bottom: 8px; }}
                .k    {{ font-weight: 600; display:inline-block; width:220px; }}
                a     {{ color:#0b5ed7; }}
            </style>
        </head>
        <body>
            <div class="box">
                <h2>New Driver Added</h2>

                <div class="row"><span class="k">User App ID:</span>{user_app_id}</div>
                <div class="row"><span class="k">Driver Name:</span>{driver_name_raw}</div>
                <div class="row"><span class="k">Driver Number:</span>{driver_number}</div>
                <div class="row"><span class="k">DOB:</span>{driver_dob}</div>
                <div class="row"><span class="k">Gender:</span>{driver_gender}</div>
                <div class="row"><span class="k">City:</span>{driver_city}</div>

                <hr>

                <div class="row"><span class="k">Driver License:</span>
                    <a href="{driver_license_url}" target="_blank">{driver_license_url}</a>
                </div>

                <div class="row"><span class="k">Driver Document:</span>
                    <a href="{driver_document_url}" target="_blank">{driver_document_url}</a>
                </div>

                <div class="row"><span class="k">Driver Photo:</span>
                    <a href="{driver_photo_url}" target="_blank">{driver_photo_url}</a>
                </div>

                <hr>
                <div class="row">Added On: {table_timestamp}</div>
            </div>
        </body>
        </html>
        '''

        try:
            send_email(
                message=html,
                subject=EMAIL_SUBJECT,
                from_address=EMAIL_FROM,
                from_name=EMAIL_FROM_NAME,
                to_address=EMAIL_TO,
                to_name=EMAIL_TO_NAME
            )
        except Exception:
            pass  # same PHP behavior

        return EmailErrorResponse(message="INSERTED")

    except ValueError as e:
        db.rollback()
        for url in new_blob_urls:
            try:
                azure_blob_delete_by_url(url)
            except:
                pass
        return EmailErrorResponse(message=str(e))

    except SQLAlchemyError as e:
        db.rollback()
        for url in new_blob_urls:
            try:
                azure_blob_delete_by_url(url)
            except:
                pass
        return EmailErrorResponse(message="ERROR_INSERT", error=str(e))

    except Exception as e:
        db.rollback()
        for url in new_blob_urls:
            try:
                azure_blob_delete_by_url(url)
            except:
                pass
        return EmailErrorResponse(message="ERROR", error=str(e))
# def get_all_driver_for_vendor(db: Session, userappid : str, limit : int = 50, offset : int = 0):    
    
#     try :
#         drivers = db.query(
#             DriverDetail.DDID,
#             DriverDetail.userAppId,
#             DriverDetail.driverName,
#             DriverDetail.driverNumber,
#             DriverDetail.driverDOB,
#             DriverDetail.driverGender,
#             DriverDetail.driverCity,
#             DriverDetail.driverLicense,
#             DriverDetail.driverDocument,
#             DriverDetail.driverPhoto,
#             DriverDetail.tableTimestamp
#         ).filter(
#             DriverDetail.userAppId == userappid
#         ).order_by(
#             DriverDetail.tableTimestamp.asc()
#         ).limit(limit).offset(offset).all()

#         if not drivers:
#             return ErrorResponse(message="NO_DRIVERS_FOUND")
        
#         return [DriverDetailResponse(
#             DRIVERID=ddid,
#             USERAPPID=user_app_id,
#             DRIVERNAME=driver_name,
#             DRIVERNUMBER=driver_number,
#             DRIVERDOB=driver_dob.strftime('%Y-%m-%d'),
#             GENDER=driver_gender,
#             DRIVERCITY=driver_city,
#             LICENSE_URL=driver_license,
#             DOCUMENT_URL=driver_document,
#             PHOTO_URL=driver_photo,
#             ADDEDON=table_timestamp.strftime('%Y-%m-%d %H:%M:%S')
#         ) for ddid, user_app_id, driver_name, driver_number, driver_dob, driver_gender, driver_city, driver_license, driver_document, driver_photo, table_timestamp in drivers]
    
#     except SQLAlchemyError:
#         return ErrorResponse(message="ERROR_RESPONSE")
#     finally:
#         db.close()


def get_all_driver_for_vendor(
    db: Session,
    userappid: str,
    limit: int = 50,
    offset: int = 0
):
    try:
        if not userappid or str(userappid).strip() == "":
            return ErrorResponse(message="ERROR_MISSING_USERAPPID")

        userappid = str(userappid).strip()

        try:
            limit = int(limit)
        except Exception:
            limit = 50

        try:
            offset = int(offset)
        except Exception:
            offset = 0

        if limit <= 0 or limit > 500:
            limit = 50
        if offset < 0:
            offset = 0

        drivers = (
            db.query(
                DriverDetail.DDID,
                DriverDetail.userAppId,
                DriverDetail.driverName,
                DriverDetail.driverNumber,
                DriverDetail.driverDOB,
                DriverDetail.driverGender,
                DriverDetail.driverCity,
                DriverDetail.driverLicense,
                DriverDetail.driverDocument,
                DriverDetail.driverPhoto,
                DriverDetail.tableTimestamp
            )
            .filter(DriverDetail.userAppId == userappid)
            .order_by(DriverDetail.tableTimestamp.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

        if not drivers:
            return ErrorResponse(message="NO_DRIVERS_FOUND")

        return [
            DriverDetailResponse(
                DRIVERID=ddid,
                USERAPPID=user_app_id,
                DRIVERNAME=driver_name,
                DRIVERNUMBER=driver_number,
                DRIVERDOB=driver_dob.strftime("%Y-%m-%d") if driver_dob else None,
                GENDER=driver_gender,
                DRIVERCITY=driver_city,
                LICENSE_URL=driver_license,
                DOCUMENT_URL=driver_document,
                PHOTO_URL=driver_photo,
                ADDEDON=table_timestamp.strftime("%Y-%m-%d %H:%M:%S") if table_timestamp else None,
            )
            for (
                ddid,
                user_app_id,
                driver_name,
                driver_number,
                driver_dob,
                driver_gender,
                driver_city,
                driver_license,
                driver_document,
                driver_photo,
                table_timestamp,
            ) in drivers
        ]

    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")


def get_all_drivers(db: Session, limit: int = 50, offset: int = 0):
    try:
        drivers = db.query(
            DriverDetail.DDID,
            DriverDetail.userAppId,
            DriverDetail.driverName,
            DriverDetail.driverNumber,
            DriverDetail.driverDOB,
            DriverDetail.driverGender,
            DriverDetail.driverCity,
            DriverDetail.driverLicense,
            DriverDetail.driverDocument,
            DriverDetail.driverPhoto,
            DriverDetail.tableTimestamp,
            User.fullName.label("vendorName")
        ).outerjoin(User, User.userAppId == DriverDetail.userAppId
        ).filter(DriverDetail.userAppId != "123456789").order_by(DriverDetail.tableTimestamp.asc()).limit(limit).offset(offset).all()

        if not drivers:
            return ErrorResponse(message="NO DRIVERS FOUND")
        
        # Convert to list of dicts for response_model
        result = []
        for d in drivers:
            result.append({
                "DDID": d.DDID,
                "USERAPPID": d.userAppId,
                "DRIVERNAME": d.driverName,
                "DRIVERNUMBER": d.driverNumber,
                "DRIVERDOB": str(d.driverDOB) if d.driverDOB else None,
                "DRIVERGENDER": d.driverGender,
                "DRIVERCITY": d.driverCity,
                "DRIVERLICENSE": d.driverLicense,
                "DRIVERDOCUMENT": d.driverDocument,
                "DRIVERPHOTO": d.driverPhoto,
                "TABLETIMESTAMP": d.tableTimestamp,
                "VENDORNAME": d.vendorName
            })

        return result
    except SQLAlchemyError as e:
        print(f"Error in get_all_drivers: {e}")
        return {"message": "DATABASE_ERROR", "data": []}
    finally:
        db.close()

def upload_driver_document_backend(
        db:Session,
       data:UploadDriverDocumentRequest
):
    try : 
        driver = db.query(DriverDetail).filter(DriverDetail.DDID == data.driverId).first()
        if not driver:
            return EmailErrorResponse(message="ERROR", error="DRIVER_NOT_FOUND")
        
        doc_meta = {
            "DRIVERLICENSE": {
                "column": "driverLicense",
                "slug": "DriverLicense",
                "public": True,
            },
            "DRIVERDOCUMENT": {
                "column": "driverDocument",
                "slug": "DriverDocument",
                "public": False,
            },
            "DRIVERPHOTO": {
                "column": "driverPhoto",
                "slug": "DriverPhoto",
                "public": True,
            },
        }

        if data.docType not in doc_meta:
            return ErrorResponse(message="ERROR", error="ERROR_INVALID_DOCTYPE")

        column_name = doc_meta[data.docType]["column"]
        slug = doc_meta[data.docType]["slug"]
        make_public = doc_meta[data.docType]["public"]

        user_app_id = driver.userAppId or ""
        driver_name = driver.driverName or ""
        driver_number = driver.driverNumber or ""
        old_url = getattr(driver, column_name, None)

        # 🔹 Same sanitization as PHP
        safe_user = re.sub(r"[^A-Za-z0-9_\-]", "", user_app_id)

        driver_folder = re.sub(r"[^A-Za-z0-9_\- ]", "", driver_name)
        driver_folder = re.sub(r"\s+", "_", driver_folder).strip()

        if not driver_folder:
            driver_folder = "Driver"

        base_blob = f"{safe_user}/drivers/{driver_folder}/"
        blob_name = f"{base_blob}{slug}_{driver_number}"

        ok, new_url = azure_blob_upload(
            blob_name=blob_name,
            base64_data=data.uploadFile,
            make_public=make_public,
        )

        if not ok:
            return ErrorResponse(
                message="ERROR",
                error=new_url or "ERROR_UPLOAD_AZURE",
            )

        setattr(driver, column_name, new_url)
        driver.tableTimestamp = datetime.now()

        db.commit()

        if old_url and old_url != new_url:
            azure_blob_delete_by_url(old_url)

        return UploadDriverDocumentResponse(
            status="SUCCESS",
            driverId=data.driverId,
            docType=data.docType,
            column=column_name,
            url=new_url,
            userAppId=user_app_id,
            driverName=driver_name,
            driverNumber=driver_number,
        )

    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="ERROR", error=str(e))
    except Exception as e:
        db.rollback()
        return ErrorResponse(message="ERROR", error=str(e))
