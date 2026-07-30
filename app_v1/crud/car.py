from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..models.car_type_details import CarTypeDetail
from ..schemas.car_type_details import CarType,CarTypeDetailResponse
from ..models.vendor_car_types import VendorCarType
from ..schemas.vendor_car_types import VendorCarTypeDetail    
from ..models.car_details import CarDetail
from ..models.user_table import User
from ..schemas.car_details import (CarDetailsResponse,NoCarDetailsResponse,CarDetailsDelete,
                                   CarDetailsCreate,UpdateCarApprovalStatusRequest,
                                   UploadCarDocumentRequest,UploadCarDocumentResponse)
from ..utils.common import ErrorResponse,EmailErrorResponse      
from ..utils.email import send_email  
from ..utils.image import upload_image,azure_blob_upload,azure_blob_delete_by_url
from datetime import datetime     
import html              
import os
import re         


def get_all_car_types(db : Session):
    try:
        car_types = db.query(CarTypeDetail).all()
        return [CarTypeDetailResponse(
            CTD=car_type.CTD,
            CARTYPE=car_type.car_type,
            CARSUBTYPE=car_type.car_sub_type,
            CAPACITY=car_type.capacity,
            IMAGEURL=car_type.image_url
        ) for car_type in car_types]
    except SQLAlchemyError:
        return NoCarDetailsResponse(message="ERROR_PREPARE")
    finally:
        db.close()


def get_vendor_car_types(db:Session):
    try:
        vendor_car_types = db.query(
            VendorCarType.VCRTID,
            VendorCarType.manufacturer,
            VendorCarType.model,
            VendorCarType.variant,
            VendorCarType.year,
            VendorCarType.fuelType,
            VendorCarType.seatingCapacity,
            VendorCarType.CTD,
            CarTypeDetail.car_type,
            CarTypeDetail.car_sub_type,
            CarTypeDetail.capacity,
            CarTypeDetail.image_url
        ).outerjoin(CarTypeDetail, VendorCarType.CTD 
                    == CarTypeDetail.CTD).order_by(
                        VendorCarType.manufacturer.asc(),
                        VendorCarType.model.asc(),
                        VendorCarType.year.desc(),
                        VendorCarType.variant.asc()
                    ).all()
        
        return [VendorCarTypeDetail(
            vcrtid=vcr_tid,
            manufacturer=manufacturer,
            model=model,
            variant=variant,
            year=year,
            fuelType=fuel_type,
            seatingCapacity=seating_capacity,
            CTD=ctd,
            cartype=car_type,
            carSubType=car_sub_type,
            capacity=capacity,
            imageUrl=image_url
        ) for(
            vcr_tid,
            manufacturer,
            model,
            variant,
            year,
            fuel_type,
            seating_capacity,
            ctd,
            car_type,
            car_sub_type,
            capacity,
            image_url
        ) in vendor_car_types
        
        ]
    except SQLAlchemyError:
        return NoCarDetailsResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    
def get_approved_car_for_vendor(db: Session, userapp_id : str, limit: int = 50, offset: int = 0):    
    
    limit = max(1, min(limit, 500))  # Enforce limit between 1 and 500
    offset = max(0, offset)  # Enforce non-negative offset

    try : 
        approved_cars = db.query(
            CarDetail, CarTypeDetail.car_type,CarTypeDetail.car_sub_type,CarTypeDetail.capacity,CarTypeDetail.image_url
        ).outerjoin(
            CarTypeDetail, CarDetail.CTD == CarTypeDetail.CTD
        ).filter(
            (CarDetail.userAppId == userapp_id) &
            (CarDetail.adminApproved == 1)
        ).order_by(
            CarDetail.registeredOn
        ).limit(limit).offset(offset).all()

        if not approved_cars:
            return NoCarDetailsResponse(message="NO_VEHICLES_FOUND")
        
        return [
            CarDetailsResponse(
                CARID=car.CARID,
                USERAPPID=car.userAppId,
                CARREGNO=car.carRegNo,
                CARMODEL=car.carModel,
                MODELYEAR=car.modelYear,
                CARCOLOR=car.carColor,
                OWNERNAME=car.ownerName,
                REGISTRATIONDOC=car.registrationDoc,
                POWEROFATTORNEYDOC=car.powerOfAttorneyDoc,
                CAROWNEDBYSAMEVENDOR=car.carOwnedBySameVendor,
                ADMINAPPROVED=car.adminApproved,
                REGISTEREDON=car.registeredOn,
                CTD=car.CTD,
                CAR_TYPE=car_type,
                CAR_SUB_TYPE=car_sub_type,
                CAPACITY=capacity,
                IMAGE_URL=image_url,
                VEHICLE_FRONT=car.imageVehicleFront,
                VEHICLE_SIDE=car.imageVehicleSide
            ) for car,car_type,car_sub_type,capacity,image_url in approved_cars]
    
    except SQLAlchemyError:
        return NoCarDetailsResponse(message="ERROR_PREPARE")
    finally:
        db.close()

def delete_car_by_id(db: Session, car_data : CarDetailsDelete):
    try:
        with db.begin():
            car = db.query(CarDetail).filter(CarDetail.CARID == car_data.CARID).first()
            if not car:
                return ErrorResponse(message="NOT_FOUND")
            
            car_details = {
                "CARID" : car.CARID,
                "userAppId" : car.userAppId,
                "carRegNo" : car.carRegNo,
                "carModel" : car.carModel,
                "modelYear" : str(car.modelYear) if car.modelYear else "N/A",
                "ownerName" : car.ownerName or "N/A",
                "CTD" : str(car.CTD) if car.CTD else "N/A",
                "registrationDoc" : car.registrationDoc,
                "powerOfAttorneyDoc" : car.powerOfAttorneyDoc if car.powerOfAttorneyDoc else "N/A",
                "carOwnedBySameVendor" : bool(car.carOwnedBySameVendor),
                "adminApproved" : bool(car.adminApproved),
                "registeredOn" : car.registeredOn.strftime('%Y-%m-%d %H:%M:%S') if car.registeredOn else "N/A",                
            }

            # DELETE CAR ID

            db.delete(car)
            db.commit()            
            
             #Send Email 
            ist_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            registration_doc_html = f'<a href="{html.escape(car_details["registrationDoc"])}" target="_blank">{html.escape(car_details["registrationDoc"])}</a>' if car_details["registrationDoc"] else '<span class="muted">Not provided</span>'
            power_of_attorney_html = f'<a href="{html.escape(car_details["powerOfAttorneyDoc"])}" target="_blank">{html.escape(car_details["powerOfAttorneyDoc"])}</a>' if car_details["powerOfAttorneyDoc"] else '<span class="muted">Not provided</span>'
            reason_html = f'<div class="code">{html.escape(car_data.reason.replace("\n", "<br>"))}</div>' if car_data.reason else '<span class="muted">Not provided</span>'
            deleted_by_html = html.escape(car_data.deletedBy) if car_data.deletedBy else '<span class="muted">Not provided</span>'

            html_content = f"""
                <html>
                <head>
                    <title>Vehicle Deleted - OpenBid</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
                        .wrap {{ max-width: 760px; margin: 24px auto; }}
                        .box  {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
                        h2    {{ margin: 0 0 12px; color: #1f2d3d; }}
                        .grid {{ display: grid; grid-template-columns: 220px 1fr; gap: 8px 14px; }}
                        .k    {{ font-weight: 600; color: #2c3e50; }}
                        .sep  {{ height: 1px; background: #eceff4; margin: 14px 0; }}
                        a     {{ color: #0b5ed7; text-decoration: none; }}
                        .muted{{ color: #6b7280; }}
                        .code {{ background:#f3f4f6; padding: 8px 10px; border-radius:8px; }}
                    </style>
                </head>
                <body>
                    <div class="wrap">
                        <div class="box">
                            <h2>Vehicle Deleted</h2>
                            <div class="grid">
                                <div class="k">CARID</div><div>{html.escape(str(car_details["CARID"]))}</div>
                                <div class="k">User App ID</div><div>{html.escape(str(car_details["userAppId"]))}</div>
                                <div class="k">Car Number</div><div>{html.escape(str(car_details["carRegNo"]))}</div>
                                <div class="k">Car Model</div><div>{html.escape(str(car_details["carModel"]))}</div>
                                <div class="k">Model Year</div><div>{html.escape(str(car_details["modelYear"]))}</div>
                                <div class="k">Owner Name</div><div>{html.escape(str(car_details["ownerName"]))}</div>
                                <div class="k">CTD</div><div>{html.escape(car_details["CTD"])}</div>
                            </div>
                            <div class="sep"></div>
                            <div class="grid">
                                <div class="k">Vehicle RC</div><div>{registration_doc_html}</div>
                                <div class="k">Power of Attorney</div><div>{power_of_attorney_html}</div>
                            </div>
                            <div class="sep"></div>
                            <div class="grid">
                                <div class="k">Car Owned by Same Vendor</div><div>{'Yes' if car_details["carOwnedBySameVendor"] else 'No'}</div>
                                <div class="k">Admin Approved</div><div>{'Yes' if car_details["adminApproved"] else 'No'}</div>
                                <div class="k">Registered On (IST)</div><div>{html.escape(car_details["registeredOn"])}</div>
                                <div class="k">Deleted At (IST)</div><div>{html.escape(ist_now)}</div>
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
                subject="OpenBid | Vehicle Deleted",
                from_address="customersupport@wizzride.com",
                from_name="WizzRide",
                to_address="openbidresourceteam@wizzride.com",
                to_name="OpenBid Resource Team"
            )

            if email_result["message"] != "SENT":
                pass

            return ErrorResponse(message="DELETED")

            
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_FOUND",error=str(e))
    finally:
        db.close()
    
# def insert_car_details(db:Session, create_car:CarDetailsCreate):
#     """
#     Insert car details into cardetails table and save base64 images using upload_image.
#     """
#     # Configuration

#     BASE_LOCAL_DIR = os.path.join(os.path.dirname(__file__),'..','vendorDocuments')
#     BASE_PUBLIC_URL = 'http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/'

#     # BASE_LOCAL_DIR = os.path.join(os.path.dirname(__file__),'..','/vendorDocuments')
#     # BASE_PUBLIC_URL = 'http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/'
#     EMAIL_SUBJECT = 'OpenBid | New Vehicle Added'
#     EMAIL_FROM = 'customersupport@wizzride.com'
#     EMAIL_FROM_NAME = 'WizzRide'
#     EMAIL_TO = 'openbidresourceteam@wizzride.com'
#     EMAIL_TO_NAME = 'OpenBid Resource Team'

#     try :
#         #Validate User App Id
#         user = db.query(User).filter(
#             User.userAppId == create_car.userAppId,
#             User.alsoVendor == True,
#             User.vendorApproved == True
#             ).first()
#         if not user:
#             return EmailErrorResponse(message="ERROR_INVALID_USERAPPID")
        
#         # Process images using upload_image
#         timestamp = datetime.now().strftime("%Y%M%D_%H%M%S")
#         car_dir = os.path.join(BASE_LOCAL_DIR,create_car.userAppId,create_car.carRegNo)
#         base_url = f"{BASE_PUBLIC_URL}{create_car.userAppId}/{create_car.carRegNo}"

#         #Vehicle RC (Required)            
#         rc_result = upload_image(
#             create_car.registrationDoc,
#             base_dir=car_dir,
#             file_stem=f"VehicleRC_{timestamp}",
#             base_url=base_url
#         )
#         if rc_result["message"] != "UPLOADED":
#             return EmailErrorResponse(message=rc_result["message"],error=rc_result.get("error"))
#         url_rc = rc_result["url"]

#         #Power of Attorney (POA - Optional)
#         url_poa = None
#         if create_car.powerOfAttorneyDoc:
#             poa_result = upload_image(
#                 create_car.registrationDoc,
#                 base_dir=car_dir,
#                 file_stem=f"PowerOfAttorney_{timestamp}",
#                 base_url=base_url
#             )

#             if poa_result["message"] != "UPLOADED":
#                 return EmailErrorResponse(message=poa_result["message"], error=poa_result.get("error"))
#             url_poa = poa_result["url"]            

#         #Vehicle Image Front (Optional)
#         url_front = None
#         if create_car.imageVehicleFront:
#             front_result = upload_image(
#                 create_car.imageVehicleFront,
#                 base_dir=car_dir,
#                 file_stem=f"VehicleFront_{timestamp}",
#                 base_url=base_url
#             )
#             if front_result["message"] != "UPLOADED":
#                 return EmailErrorResponse(message=front_result["message"], error=front_result.get("error"))
#             url_front = front_result["url"]
        
#         #Vehicle Image Side (Optional)
#         url_side = None
#         if create_car.imageVehicleSide:
#             side_result = upload_image(
#                 create_car.imageVehicleSide,
#                 base_dir=car_dir,
#                 file_stem=f"VehicleSide_{timestamp}",
#                 base_url=base_url
#             )
#             if side_result["message"] != "UPLOADED":
#                 return EmailErrorResponse(message=side_result["message"],error=side_result.get("error"))
#             url_side=side_result["url"]

#         #Derived Fields
#         same_vendor = not bool(url_poa)
#         admin_approved = False
#         registered_on = datetime.now()

#         # Check for duplicate car
#         existing_car = db.query(CarDetail).filter(
#             CarDetail.userAppId == create_car.userAppId,
#             CarDetail.carRegNo == create_car.carRegNo
#         ).first()
#         if existing_car:
#             return EmailErrorResponse(message="ERROR_ALREADY_EXISTS")
        
#         new_car = CarDetail(
#             userAppId=create_car.userAppId,
#             carRegNo=create_car.carRegNo,
#             carColor=create_car.carColor,
#             carModel=create_car.carModel,
#             modelYear=create_car.modelYear,
#             carOwnedBySameVendor=same_vendor,
#             CTD=create_car.CTD,
#             ownerName=create_car.ownerName,
#             registrationDoc=url_rc,
#             powerOfAttorneyDoc=url_poa,
#             registeredOn=registered_on,
#             imageVehicleFront=url_front,
#             imageVehicleSide=url_side,
#             adminApproved=admin_approved
#         )
#         db.add(new_car)
#         db.commit()        

#         html_content = f"""
#         <html>
#         <head>
#             <title>New Vehicle Added - OpenBid</title>
#             <style>
#                 body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
#                 .wrap {{ max-width: 700px; margin: 24px auto; }}
#                 .box {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
#                 h2 {{ margin: 0 0 12px; color: #1f2d3d; }}
#                 .grid {{ display: grid; grid-template-columns: 240px 1fr; gap: 8px 14px; }}
#                 .k {{ font-weight: 600; color: #2c3e50; }}
#                 .sep {{ height: 1px; background: #eceff4; margin: 14px 0; }}
#                 a {{ color: #0b5ed7; text-decoration: none; }}
#                 .muted {{ color: #6b7280; }}
#             </style>
#         </head>
#         <body>
#             <div class="wrap">
#                 <div class="box">
#                     <h2>New Vehicle Added</h2>
#                     <div class="grid">
#                         <div class="k">User App ID</div><div>{create_car.userAppId}</div>
#                         <div class="k">Car Number</div><div>{create_car.carRegNo}</div>
#                         <div class="k">Car Model</div><div>{create_car.carModel}</div>
#                         <div class="k">Model Year</div><div>{create_car.modelYear}</div>
#                         <div class="k">Car Color</div><div>{create_car.carColor}</div>
#                         <div class="k">Owner Name</div><div>{create_car.ownerName}</div>
#                         <div class="k">CTD</div><div>{create_car.CTD}</div>
#                     </div>
#                     <div class="sep"></div>
#                     <div class="grid">
#                         <div class="k">Vehicle RC</div><div><a href="{url_rc}" target="_blank">{url_rc}</a></div>
#                         <div class="k">Power of Attorney</div><div>{'<a href="' + url_poa + '" target="_blank">' + url_poa + '</a>' if url_poa else '<span class="muted">Not provided</span>'}</div>
#                         <div class="k">Vehicle Front</div><div>{'<a href="' + url_front + '" target="_blank">' + url_front + '</a>' if url_front else '<span class="muted">Not provided</span>'}</div>
#                         <div class="k">Vehicle Side</div><div>{'<a href="' + url_side + '" target="_blank">' + url_side + '</a>' if url_side else '<span class="muted">Not provided</span>'}</div>
#                     </div>
#                     <div class="sep"></div>
#                     <div class="grid">
#                         <div class="k">Car Owned by Same Vendor</div><div>{'Yes' if same_vendor else 'No'}</div>
#                         <div class="k">Admin Approved</div><div>{'Yes' if admin_approved else 'Pending'}</div>
#                         <div class="k">Registered On (IST)</div><div>{registered_on.strftime('%Y-%m-%d %H:%M:%S')}</div>
#                     </div>
#                     <div class="sep"></div>
#                     <div class="muted">This is an automated message. Please do not reply.</div>
#                 </div>
#             </div>
#         </body>
#         </html>
#         """
#         try : 
#             email_result = send_email(
#                 message=html_content,
#                 subject=EMAIL_SUBJECT,
#                 from_address=EMAIL_FROM,
#                 from_name=EMAIL_FROM_NAME,
#                 to_address=EMAIL_TO,
#                 to_name=EMAIL_TO_NAME            
#             )
#         except Exception as e: 
#             EmailErrorResponse(message="MAIL_ERROR")

#         return EmailErrorResponse(message="INSERTED")
                
#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_INVALID",error=str(e))
#     except Exception as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR",error=str(e))
#     finally:
#         db.close()


def insert_car_details(db: Session, create_car: CarDetailsCreate):
    EMAIL_SUBJECT = "OpenBid | New Vehicle Added"
    EMAIL_FROM = "ticketdetails@wizzride.com"
    EMAIL_FROM_NAME = "WizzRide"
    EMAIL_TO = "openbidresourceteam@wizzride.com"
    EMAIL_TO_NAME = "OpenBid Resource Team"

    def clean(v) -> str:
        return str(v or "").strip()

    tz = ZoneInfo("Asia/Kolkata")

    # ----------------------------------
    # REQUIRED FIELD VALIDATION (same PHP)
    # ----------------------------------
    required_fields = {
        "userAppId": getattr(create_car, "userAppId", None),
        "carRegNo": getattr(create_car, "carRegNo", None),
        "carModel": getattr(create_car, "carModel", None),
        "modelYear": getattr(create_car, "modelYear", None),
        "carColor": getattr(create_car, "carColor", None),
        "ownerName": getattr(create_car, "ownerName", None),
        "imageVehicleRC": getattr(create_car, "registrationDoc", None),
        "CTD": getattr(create_car, "CTD", None),
        "imageVehicleFront": getattr(create_car, "imageVehicleFront", None),
        "imageVehicleSide": getattr(create_car, "imageVehicleSide", None),
    }

    for field_name, field_value in required_fields.items():
        if field_value is None or clean(field_value) == "":
            return EmailErrorResponse(message=f"ERROR_MISSING_{field_name.upper()}")

    # ----------------------------------
    # CLEAN INPUT (same PHP)
    # ----------------------------------
    user_app_id = clean(create_car.userAppId).replace(" ", "")
    car_reg_no = clean(create_car.carRegNo).replace(" ", "").upper()
    car_reg_no_raw = clean(create_car.carRegNo)
    car_model = clean(create_car.carModel)
    owner_name = clean(create_car.ownerName)
    car_color = clean(create_car.carColor).upper()
    ctd_raw = clean(create_car.CTD)

    try:
        model_year = int(create_car.modelYear)
    except Exception:
        return EmailErrorResponse(message="ERROR_INVALID_MODELYEAR")

    if model_year < 1990 or model_year > (datetime.now().year + 1):
        return EmailErrorResponse(message="ERROR_INVALID_MODELYEAR")

    registered_on = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    new_blob_urls: list[str] = []

    try:
        with db.begin():
            # PHP only checks existence in cardetails; existing Python checked approved vendor.
            # Keep vendor validity check because this is part of your project behavior.
            user = db.query(User).filter(
                User.userAppId == user_app_id,
                User.alsoVendor == True,
                User.vendorApproved == True
            ).first()

            if not user:
                return EmailErrorResponse(message="ERROR_INVALID_USERAPPID")

            existing = db.query(CarDetail).filter(
                CarDetail.userAppId == user_app_id,
                CarDetail.carRegNo == car_reg_no
            ).first()
            if existing:
                return EmailErrorResponse(message="ERROR_ALREADY_EXISTS")

            base_blob = f"{user_app_id}/{car_reg_no}/"

            # RC (PRIVATE)
            ok_rc, url_rc = azure_blob_upload(
                blob_name=f"{base_blob}VehicleRC",
                base64_data=create_car.registrationDoc,
                make_public=False
            )
            if not ok_rc:
                raise ValueError("ERROR_SAVE_RC")
            new_blob_urls.append(url_rc)

            # POA (PRIVATE, optional)
            url_poa = None
            if create_car.powerOfAttorneyDoc and clean(create_car.powerOfAttorneyDoc) != "":
                ok_poa, url_poa = azure_blob_upload(
                    blob_name=f"{base_blob}PowerOfAttorney",
                    base64_data=create_car.powerOfAttorneyDoc,
                    make_public=False
                )
                if not ok_poa:
                    raise ValueError("ERROR_SAVE_POA")
                new_blob_urls.append(url_poa)

            # FRONT (PUBLIC)
            ok_front, url_front = azure_blob_upload(
                blob_name=f"{base_blob}VehicleFront",
                base64_data=create_car.imageVehicleFront,
                make_public=True
            )
            if not ok_front:
                raise ValueError("ERROR_SAVE_FRONT")
            new_blob_urls.append(url_front)

            # SIDE (PUBLIC)
            ok_side, url_side = azure_blob_upload(
                blob_name=f"{base_blob}VehicleSide",
                base64_data=create_car.imageVehicleSide,
                make_public=True
            )
            if not ok_side:
                raise ValueError("ERROR_SAVE_SIDE")
            new_blob_urls.append(url_side)

            car_owned_by_same_vendor = 0 if url_poa else 1
            admin_approved = 0

            new_car = CarDetail(
                userAppId=user_app_id,
                carRegNo=car_reg_no,
                carModel=car_model,
                modelYear=model_year,
                carColor=car_color,
                ownerName=owner_name,
                CTD=int(create_car.CTD),
                registrationDoc=url_rc,
                powerOfAttorneyDoc=url_poa,
                imageVehicleFront=url_front,
                imageVehicleSide=url_side,
                carOwnedBySameVendor=car_owned_by_same_vendor,
                adminApproved=admin_approved,
                registeredOn=registered_on
            )
            db.add(new_car)

        # ----------------------------------
        # EXACT PHP EMAIL HTML
        # ----------------------------------
        html = f'''
        <html>
        <head>
            <title>New Vehicle Added - OpenBid</title>
            <style>
                body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
                .wrap {{ max-width: 700px; margin: 24px auto; }}
                .box  {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
                h2    {{ margin: 0 0 12px; color: #1f2d3d; }}
                .grid {{ display: grid; grid-template-columns: 240px 1fr; gap: 8px 14px; }}
                .k    {{ font-weight: 600; color: #2c3e50; }}
                .sep  {{ height: 1px; background: #eceff4; margin: 14px 0; }}
                a     {{ color: #0b5ed7; text-decoration: none; }}
                .muted{{ color: #6b7280; }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <div class="box">
                    <h2>New Vehicle Added</h2>
                    <div class="grid">
                        <div class="k">User App ID</div><div>{user_app_id}</div>
                        <div class="k">Car Number</div><div>{car_reg_no_raw}</div>
                        <div class="k">Car Model</div><div>{car_model}</div>
                        <div class="k">Model Year</div><div>{model_year}</div>
                        <div class="k">Car Color</div><div>{car_color}</div>
                        <div class="k">Owner Name</div><div>{owner_name}</div>
                        <div class="k">CTD</div><div>{ctd_raw}</div>
                    </div>
                    <div class="sep"></div>
                    <div class="grid">
                        <div class="k">Vehicle RC</div><div><a href="{url_rc}" target="_blank">{url_rc}</a></div>
                        <div class="k">Power of Attorney</div><div>{f'<a href="{url_poa}" target="_blank">{url_poa}</a>' if url_poa else '<span class="muted">Not provided</span>'}</div>
                        <div class="k">Vehicle Front</div><div>{f'<a href="{url_front}" target="_blank">{url_front}</a>' if url_front else '<span class="muted">Not provided</span>'}</div>
                        <div class="k">Vehicle Side</div><div>{f'<a href="{url_side}" target="_blank">{url_side}</a>' if url_side else '<span class="muted">Not provided</span>'}</div>
                    </div>
                    <div class="sep"></div>
                    <div class="grid">
                        <div class="k">Car Owned by Same Vendor</div><div>{"Yes" if car_owned_by_same_vendor else "No"}</div>
                        <div class="k">Admin Approved</div><div>{"Yes" if admin_approved else "Pending"}</div>
                        <div class="k">Registered On (IST)</div><div>{registered_on}</div>
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
                subject=EMAIL_SUBJECT,
                from_address=EMAIL_FROM,
                from_name=EMAIL_FROM_NAME,
                to_address=EMAIL_TO,
                to_name=EMAIL_TO_NAME
            )
        except Exception:
            pass

        return EmailErrorResponse(message="INSERTED")

    except ValueError as e:
        db.rollback()
        for url in new_blob_urls:
            try:
                azure_blob_delete_by_url(url)
            except Exception:
                pass
        return EmailErrorResponse(message=str(e))

    except SQLAlchemyError as e:
        db.rollback()
        for url in new_blob_urls:
            try:
                azure_blob_delete_by_url(url)
            except Exception:
                pass
        return EmailErrorResponse(message="ERROR", error=str(e))

    except Exception as e:
        db.rollback()
        for url in new_blob_urls:
            try:
                azure_blob_delete_by_url(url)
            except Exception:
                pass
        return EmailErrorResponse(message="ERROR", error=str(e))


def get_all_cars(db: Session):
    try:
        cars = db.query(CarTypeDetail).all()

        if not cars:
            return NoCarDetailsResponse(message="NO CARS FOUND")
        
        result = []
        for car in cars :
            result.append({
                "CARID": car.CARID,
                "USERAPPID" :car.userAppId,
                "CARREGNO" : car.carRegNo,
                "MODELYEAR" : car.modelYear,
                "CARCOLOR" : car.carColor,
                "OWNERNAME" : car.ownerName,
                "REGISTRATIONDOC" : car.registrationDoc,
                "POWEROFATTORNEYDOC" : car.powerOfAttorneyDoc,
                "REGISTEREDON" : car.registeredOn.strftime('%Y-%m-%d %H:%M:%S') if car.registeredOn else None,
                "ADMINAPPROVED" : bool(car.adminApproved),
                "CAROWNEDBYSAMEVENDOR" : bool(car.carOwnedBySameVendor),                
                "CTD" : car.CTD,
                "IMAGEVEHICLEFRONT" : car.imageVehicleFront,
                "IMAGEVEHICLESIDE" : car.imageVehicleSide
            })

        return result
    except SQLAlchemyError as e:
        return NoCarDetailsResponse(message="ERROR_PREPARE",error=str(e))
    finally:
        db.close()

def update_car_approval_status(db: Session, data : UpdateCarApprovalStatusRequest):
    try:
        car = db.query(CarDetail).filter(CarDetail.CARID == data.CARID).first()
        if not car:
            return ErrorResponse(message="NO ROW UPDATED")
        
        # Avoid unnecessary DB write
        if car.adminApproved == data.adminApproved:
            return NoCarDetailsResponse(message="NO ROW UPDATED")
        
        car.adminApproved = data.adminApproved
        db.commit()
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="ERROR_FOUND",error=str(e))
    finally:
        db.close()


def upload_car_document_backend(
        db:Session,
        data : UploadCarDocumentRequest
):
    try:
        car = db.query(CarDetail).filter(CarDetail.CARID == data.carId).first()
        if not car:
            return ErrorResponse(message="NO ROW UPDATED")
        
        if not car:
            return ErrorResponse(message="ERROR", error="CAR_NOT_FOUND")

        doc_meta = {
            "REGISTRATIONDOC": {
                "column": "registrationDoc",
                "slug": "VehicleRC",
            },
            "POWEROFFATTORNEYDOC": {
                "column": "powerOfAttorneyDoc",
                "slug": "PowerOfAttorney",
            },
            "IMAGEVEHICLEFRONT": {
                "column": "imageVehicleFront",
                "slug": "VehicleFront",
            },
            "IMAGEVEHICLESIDE": {
                "column": "imageVehicleSide",
                "slug": "VehicleSide",
            },
        }

        if data.docType not in doc_meta:
            return ErrorResponse(message="ERROR", error="ERROR_INVALID_DOCTYPE")

        column_name = doc_meta[data.docType]["column"]
        slug = doc_meta[data.docType]["slug"]

        user_app_id = car.userAppId
        car_reg_no_raw = car.carRegNo or ""
        old_url = getattr(car, column_name, None)

        # 🔴 Same sanitization as PHP
        car_reg_no_sanitized = (
            str(car_reg_no_raw).replace(" ", "").upper()
        )

        blob_base = f"{user_app_id}/{car_reg_no_sanitized}/"
        blob_name = f"{blob_base}{slug}"

        make_public = data.docType in (
            "IMAGEVEHICLEFRONT",
            "IMAGEVEHICLESIDE",
        )

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

        setattr(car, column_name, new_url)
        car.tableTimestamp = datetime.now()

        db.commit()

        if old_url and old_url != new_url:
            azure_blob_delete_by_url(old_url)

        return UploadCarDocumentResponse(
            status="SUCCESS",
            carId=data.carId,
            docType=data.docType,
            column=column_name,
            url=new_url,
            userAppId=user_app_id,
            carRegNo=car_reg_no_sanitized,
        )

    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="ERROR", error=str(e))
    except Exception as e:
        db.rollback()
        return ErrorResponse(message="ERROR", error=str(e))