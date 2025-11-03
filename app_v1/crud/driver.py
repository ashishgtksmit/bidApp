from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
import urllib
from ..utils.common import EmailErrorResponse,ErrorResponse
from ..models.driver_details import DriverDetail
from ..schemas.driver_details import UpdateDriverDetail,DeleteDriverDetail,CreateDriverDetail,DriverDetailResponse
from ..utils.image import upload_image
from ..utils.email import send_email
import os
import re
import html
from datetime import datetime



def update_driver_details(db : Session, driver_data : UpdateDriverDetail):
    try: 
        with db.begin():    
            driver = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.DDID).first()
            if not driver:
                return ErrorResponse(message="NOT_FOUND")
            
            update_data = {
                "driverCity" : driver_data.driverCity.strip(),
                "driverNumber" : re.sub(r'\D','',driver_data.driverNumber),
                "tableTimestamp" : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            # handle photo url

            new_photo_url = None
            if driver_data.driverPhotoImg and driver_data.driverPhotoImg.strip():
                safe_user = re.sub(r'[^A-Za-z0-9_\-]','',driver.userAppId)
                driver_folder = re.sub(r'[^A-Za-z0-9_\-\s]', '', driver.driverName or 'Driver').strip() or 'Driver'
                base_dir = f"vendorDocuments/{safe_user}/drivers/{driver_folder}"
                base_url = f"http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/{safe_user}/drivers/{urllib.parse.quote(driver_folder)}"
                file_stem = f"DriverPhoto_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                upload_result = upload_image(driver_data.driverPhotoImg,base_dir,file_stem,base_url)
                if upload_result["message"] != "UPLOADED":
                    return EmailErrorResponse(message="ERROR_SAVE_DRIVER_PHOTO", error=upload_result.get("error"))
                new_photo_url = update_data["driverPhoto"] = upload_result["url"]

            # update driver
            update = db.query(DriverDetail).filter(DriverDetail.DDID == driver_data.DDID).update(update_data)    
            # db.commit()

            if update == 0:
                return EmailErrorResponse(message="ERROR_UPDATE", error="Database update failed")
            
            # Send email
            html = f"""
                <html>
                <head>
                    <title>Driver Updated - OpenBid</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; color:#333; background:#f5f7fb; }}
                        .wrap {{ max-width:760px; margin:24px auto; }}
                        .box  {{ padding:18px 20px; border:1px solid #e6e9f0; border-radius:12px; background:#fff; }}
                        h2    {{ margin:0 0 12px; color:#1f2d3d; }}
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
                                <div class="k">DRIVERID</div><div>{driver_data.DDID}</div>
                                <div class="k">User App ID</div><div>{driver.userAppId}</div>
                                <div class="k">Driver Name</div><div>{driver.driverName or 'N/A'}</div>
                                <div class="k">Old City</div><div>{driver.driverCity}</div>
                                <div class="k">New City</div><div>{driver_data.driverCity}</div>
                                <div class="k">Old Number</div><div>{driver.driverNumber}</div>
                                <div class="k">New Number</div><div>{update_data['driverNumber']}</div>
                                <div class="k">Photo</div><div>
                                    {f'<a href="{new_photo_url}" target="_blank">{new_photo_url}</a>' if new_photo_url else '<span class="muted">Unchanged</span>'}
                                </div>
                                <div class="k">Updated On (IST)</div><div>{update_data['tableTimestamp']}</div>
                            </div>
                            <div class="sep"></div>
                            <div class="muted">This is an automated message. Please do not reply.</div>
                        </div>
                    </div>
                </body>
                </html>
            """
            email_result = send_email(
                message=html,
                subject="Open Bid | Driver Updated",
                from_address="customersupport@wizzride.com",
                from_name = "WizzRide",
                to_address="openbidresourceteam@wizzride.com",
                to_name="OpenBid Resource Team"
            )

            if email_result["message"] != "SENT":
                pass
                # print(f"Email failed: {email_result.get('error')}")
                # Continue despite email failure, as in PHP
            return ErrorResponse(message="UPDATED")
            
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_FOUND",error=str(e))
    
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
    
def insert_driver(db: Session, driver_data : CreateDriverDetail):
    """
    Insert driver details into driverDetails table and save base64 images using upload_image.
    """

    #Configuration
    BASE_LOCAL_DIR = os.path.join(os.path.dirname(__file__),'..','vendorDocuments')
    BASE_PUBLIC_URL = 'http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/'
    EMAIL_SUBJECT   = 'OpenBid | New Driver Added'
    EMAIL_FROM      = 'customersupport@wizzride.com'
    EMAIL_FROM_NAME  = 'WizzRide'
    EMAIL_TO        = 'openbidresourceteam@wizzride.com'
    EMAIL_TO_NAME    = 'OpenBid Resource Team'

    #Check if Driver exists 
    existing_driver = db.query(DriverDetail).filter(
        DriverDetail.userAppId == driver_data.userAppId,
        DriverDetail.driverNumber == driver_data.driverNumber
    ).first()
    if existing_driver:
        return EmailErrorResponse(message="ERROR_ALREADY_EXISTS")
    
    # File system work stays here
    safe_user_id = re.sub(r'[^A-Za-z0-9_-]', '', driver_data.userAppId)
    driver_folder = re.sub(r'[^A-Za-z0-9_-]', '', driver_data.driverName) or 'Driver'
    driver_folder = ' '.join(driver_folder)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    driver_dir = os.path.join(BASE_LOCAL_DIR,safe_user_id,'drivers',driver_folder)
    base_url = f"{BASE_PUBLIC_URL}{safe_user_id}/drivers/{driver_folder}"


    license_result = upload_image(driver_data.driverLicenseImg,base_dir=driver_dir,file_stem=f"DriverDocument_{timestamp}",base_url=base_url)
    if license_result["message"] != "UPLOADED":
        return EmailErrorResponse(message="ERROR_INVALID_LICENSE_IMAGE",error=license_result.get("error"))
    
    document_result = upload_image(driver_data.driverDocumentImg,base_dir=driver_dir,file_stem=f"DriverDocument_{timestamp}",base_url=base_url)
    if document_result["message"] != "UPLOADED":
        return EmailErrorResponse(message="ERROR_INVALID_DOCUMENT_IMAGE",error=document_result.get("error"))
    
    photo_result = upload_image(driver_data.driverPhotoImg,base_dir=driver_dir,file_stem=f"DriverDocument_{timestamp}",base_url=base_url)
    if photo_result["message"] != "UPLOADED":
        return EmailErrorResponse(message="ERROR_INVALID_PHOTO_IMAGE",error=photo_result.get("error"))
    
    license_url = license_result["url"]
    document_url = document_result["url"]
    photo_url = photo_result["url"]

    try:
        new_driver = DriverDetail(
            userAppId=driver_data.userAppId,
            driverName=driver_data.driverName,
            driverNumber=driver_data.driverNumber,
            driverDOB=driver_data.driverDOB,
            driverGender=driver_data.driverGender,
            driverCity=driver_data.driverCity,
            driverLicense=license_url,
            driverDocument=document_url,
            driverPhoto=photo_url,
            tableTimestamp=datetime.now()
        )

        db.add(new_driver)
        db.commit()
        

        html_content = f"""
        <html>
        <head>
            <title>New Driver Added - OpenBid</title>
            <style>
                body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
                .wrap {{ max-width: 760px; margin: 24px auto; }}
                .box {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
                h2 {{ margin: 0 0 12px; color: #1f2d3d; }}
                .grid {{ display: grid; grid-template-columns: 220px 1fr; gap: 8px 14px; }}
                .k {{ font-weight: 600; color: #2c3e50; }}
                .sep {{ height: 1px; background: #eceff4; margin: 14px 0; }}
                a {{ color: #0b5ed7; text-decoration: none; }}
                .muted {{ color: #6b7280; }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <div class="box">
                    <h2>New Driver Added</h2>
                    <div class="grid">
                        <div class="k">User App ID</div><div>{driver_data.userAppId}</div>
                        <div class="k">Driver Name</div><div>{driver_data.driverName}</div>
                        <div class="k">Driver Number</div><div>{driver_data.driverNumber}</div>
                        <div class="k">Driver DOB</div><div>{driver_data.driverDOB}</div>
                        <div class="k">Gender</div><div>{driver_data.driverGender}</div>
                        <div class="k">City</div><div>{driver_data.driverCity}</div>
                        <div class="k">License Image</div><div><a href="{license_url}" target="_blank">{license_url}</a></div>
                        <div class="k">Document</div><div><a href="{document_url}" target="_blank">{document_url}</a></div>
                        <div class="k">Photo</div><div><a href="{photo_url}" target="_blank">{photo_url}</a></div>
                        <div class="k">Admin Approved</div><div>Pending</div>
                        <div class="k">Added On (IST)</div><div>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
                    </div>
                    <div class="sep"></div>
                    <div class="muted">This is an automated message. Please do not reply.</div>
                </div>
            </div>
        </body>
        </html>
        """

        try:
            email_result = send_email(
                message=html_content,
                subject=EMAIL_SUBJECT,
                from_address=EMAIL_FROM,
                from_name=EMAIL_FROM_NAME,
                to_address=EMAIL_TO,
                to_name=EMAIL_TO_NAME
            )
            print(email_result)
        except Exception as e:
            print(str(e))

        return EmailErrorResponse(message="INSERTED")
    except SQLAlchemyError as e: 
        db.rollback()
        return EmailErrorResponse(message="ERROR_INSERT",error=str(e))
    except ValueError as e : 
        db.rollback()
        return EmailErrorResponse(message="ERROR",error=str(e))

def get_all_driver_for_vendor(db: Session, userappid : str, limit : int = 50, offset : int = 0):    
    
    try :
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
            DriverDetail.tableTimestamp
        ).filter(
            DriverDetail.userAppId == userappid
        ).order_by(
            DriverDetail.tableTimestamp.asc()
        ).limit(limit).offset(offset).all()

        if not drivers:
            return ErrorResponse(message="NO_DRIVERS_FOUND")
        
        return [DriverDetailResponse(
            DRIVERID=ddid,
            USERAPPID=user_app_id,
            DRIVERNAME=driver_name,
            DRIVERNUMBER=driver_number,
            DRIVERDOB=driver_dob.strftime('%Y-%m-%d'),
            GENDER=driver_gender,
            DRIVERCITY=driver_city,
            LICENSE_URL=driver_license,
            DOCUMENT_URL=driver_document,
            PHOTO_URL=driver_photo,
            ADDEDON=table_timestamp.strftime('%Y-%m-%d %H:%M:%S')
        ) for ddid, user_app_id, driver_name, driver_number, driver_dob, driver_gender, driver_city, driver_license, driver_document, driver_photo, table_timestamp in drivers]
    
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_RESPONSE")
    finally:
        db.close()


