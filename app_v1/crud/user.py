from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..models.user_table import User
from ..models.bid_details import BidDetail
from ..models.request_type_details import RequestType
from ..models.region_details import Region
from ..models.location_details import LocationDetail
from ..models.user_table import User
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..models.tags_table import Tag
from ..schemas.user_table import (NoUserResponse,BidderDetail,UserBankDetailsResponse,
                                  UserCreate,LogoutResponse,UserDelete,UserBankDetailsUpdate,
                                  UserImageUpload,VendorUpdate,VendorResponse,VendorKycCreate, 
                                  UpdateRequestTypeSelectionsRequest,UpdateRegionCitySelectionsRequest,
                                  RequestTypeResponse,GetUserDetailsResponse)
from ..utils.common import ErrorResponse,ImageResponse,EmailErrorResponse,_ids_to_csv,_to_id_array,_csv_to_set
from ..utils.image import upload_image
from ..utils.email import send_email
from datetime import date, datetime
import re
import os
import html

def get_users_all(db:Session):
    try:
        with db.begin():
            users = db.query(User).all()
            if not users:
                return NoUserResponse(message="NO_USER")           

            return [GetUserDetailsResponse(
                USERAPPID=user.userAppId,
                ALTERNATEMNUM=user.alternateNumber,
                FULLNAME=user.fullName,
                EMAILID=user.emailId,
                DOB=user.dob,
                CITY=user.city,
                GENDER=user.gender,
                PROFILEPIC=user.profilePicture,
                RATING=user.rating,
                TOTALREVIEWS=user.totalNoOfReviews,
                FCMTOKEN=user.fcmToken,
                USERLOGINSTATUS=user.user_login_status,
                TABLETIMESTAMP=user.tableTimestamp
            )for user in users]
    except SQLAlchemyError as e : 
        db.rollback()
        return EmailErrorResponse(message="ERROR_",error=str(e))
    finally:
        db.close()
    
def get_user_details(db: Session, userAppId : int):
    try:
        users = db.query(User).filter(User.userAppId == userAppId).all()

        if not users:
            return NoUserResponse(message="NO REGISTERED")
        
        return [GetUserDetailsResponse(
            ALTERNATEMNUM=user.alternateNumber,
            FULLNAME=user.fullName,
            EMAILID=user.emailId,
            DOB=user.dob,
            CITY=user.city,
            GENDER=user.gender,
            PROFILEPIC=user.profilePicture,
            RATING=user.rating,
            TOTALREVIEWS=user.totalNoOfReviews,
            FCMTOKEN=user.fcmToken,
            USERLOGINSTATUS=user.user_login_status
        ) for user in users]
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()


def check_user(db:Session, user_app_id : str):
    try:
        users = db.query(User).filter(User.userAppId == user_app_id).first()

        if not users:
            return NoUserResponse(message="NO USERS PRESENT")
        
        return NoUserResponse(message="REGISTERED USER")
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()


def get_all_vendors(db:Session):
    try:
        vendors = db.query(User).filter(
            (User.alsoVendor == 1) & (User.vendorApproved == 1)
        ).all()

        if not vendors:
            return NoUserResponse(message="NO VENDORS FOUND")
        
        return [GetUserDetailsResponse(
            USERAPPID=vendor.userAppId,
            ALTERNATEMNUM=vendor.alternateNumber,
            FULLNAME=vendor.fullName,
            EMAILID=vendor.emailId,
            DOB=vendor.dob,
            CITY=vendor.city,
            GENDER=vendor.gender,
            PROFILEPIC=vendor.profilePicture,
            RATING=vendor.rating,
            TOTALREVIEWS=vendor.totalNoOfReviews,
            FCMTOKEN=vendor.fcmToken,
            USERLOGINSTATUS=vendor.user_login_status,
            ALSOVENDOR=vendor.alsoVendor,
            TABLETIMESTAMP=vendor.tableTimestamp

        ) for vendor in vendors]
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()

def get_vendor_by_rid(db: Session, rid: int):    
    try : 
        vendors = db.query(
            User.fullName,
            User.userAppId,
            User.alternateNumber,
            User.emailId,
            User.dob,
            User.city,
            User.rating,
            User.totalNoOfReviews,
            User.profilePicture,   # ← missing comma fixed
            BidDetail.bidderID,
            BidDetail.bidAmount,
            User.tags,
            User.noOfTripsCompleted,
            BidDetail.CARID,

            CarDetail.userAppId,
            CarDetail.carRegNo,
            CarDetail.carModel,
            CarDetail.modelYear,
            CarDetail.carColor,
            CarDetail.ownerName,
            CarDetail.registrationDoc,
            CarDetail.powerOfAttorneyDoc,
            CarDetail.registeredOn,
            CarDetail.adminApproved,
            CarDetail.carOwnedBySameVendor,
            CarDetail.CTD,
            CarDetail.imageVehicleFront,
            CarDetail.imageVehicleSide,

            CarTypeDetail.car_type,
            CarTypeDetail.car_sub_type,
            CarTypeDetail.capacity,
            CarTypeDetail.image_url
        ).join(
            User, User.userAppId == BidDetail.bidderID
        ).outerjoin(
            CarDetail, CarDetail.CARID == BidDetail.CARID
        ).outerjoin(
            CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
        ).filter(
            (BidDetail.rID == rid) &
            (BidDetail.bidStatus == 'REQUEST - CONFIRMED')
        ).all()

        if not vendors:
            return NoUserResponse(message="NO VENDOR DATA FOUND")
        
        result = []
        for(full_name, primary_number, alternate_number, email_id, dob, city,rating, total_no_of_reviews, profile_pic, 
            bidder_id, bid_amount, tags_str, no_of_trips_completed, car_id, user_app_id, car_reg_no, car_model,
            model_year, car_color, owner_name, registration_doc, power_of_attorney_doc, registered_on, admin_approved,
            car_owned_by_same_vendor, ctd, image_vehicle_front, image_vehicle_side, car_type, car_sub_type, 
            capacity, image_url) in vendors : 

            tag_ids = []   # start with an empty list
            if tags_str:   # check if tags_str is not None or empty
                # split string by "," -> gives list like ["1", "2", "3"]
                tag_parts = tags_str.split(",")

                # go through each piece
                for t in tag_parts:
                    cleaned = t.strip()   # remove spaces
                    if cleaned:          # if not empty string
                        tag_ids.append(int(cleaned))   # convert to int and add to list
            else:
                tag_ids = []

            #get tag names

            tag_names = []

            if tag_ids:
                tags_rows = db.query(Tag.tagsName).filter(
                    Tag.TAGID.in_(tag_ids)
                ).all()

                for r in tags_rows:
                    tag_names.append(r[0])

        result.append(
            BidderDetail(
                FULLNAME=full_name,
                PRIMARYNUMBER=primary_number,
                ALTERNATENUMBER=alternate_number,
                EMAILID=email_id,
                DOB=dob,
                CITY=city,
                RATING=rating,
                TOTALNOOFREVIEWS=total_no_of_reviews,
                BIDDERID=bidder_id,
                BIDDERAMOUT=bid_amount,
                PROFILEPIC=profile_pic,
                TAGS=tag_names,
                NOOFTRIPSCOMPLETED=no_of_trips_completed,
                CARID=car_id,
                CARREGNO=car_reg_no,
                CARMODEL=car_model,
                MODELYEAR=model_year,
                CARCOLOR=car_color,
                OWNERNAME=owner_name,
                REGISTRATIONDOC=registration_doc,
                POWEROFATTORNEYDOC=power_of_attorney_doc,
                REGISTEREDON=registered_on,
                ADMINAPPROVED=admin_approved,
                CAROWNEDBYSAMEVENDOR=car_owned_by_same_vendor,
                CTD=ctd,
                IMAGEVEHICLEFRONT=image_vehicle_front,
                IMAGEVEHICLESIDE=image_vehicle_side,
                CAR_USERAPPID=user_app_id,
                CAR_TYPE=car_type,
                CAR_SUB_TYPE=car_sub_type,
                CAPACITY=capacity,
                CAR_TYPE_IMAGE_URL=image_url
            ))
        
        return result
    except SQLAlchemyError:
        return NoUserResponse(message="ERROR_PREPARE")
    finally:
        db.close()
        

def get_user_bank_details(db:Session, userappid : int):
    try:
        bankdetails = db.query(
            User.bankAccountHolderName,
            User.bankAccountNo,
            User.bankIFSC,
            User.bankName
            ).filter(User.userAppId == userappid).limit(1).all()
        db.commit()
        if not bankdetails:
            return ErrorResponse(message="NO_BANK_DETAILS")
        
        bank_acc_holder_name,bank_acc_no,bank_ifsc,bank_name = bankdetails[0]
        return UserBankDetailsResponse(
                BANK_AC_HOLDER= bank_acc_holder_name,
                BANK_AC_NO = bank_acc_no,
                BANK_IFSC = bank_ifsc,
                BANK_NAME=bank_name
            )
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    
def update_password(db:Session, user_app_id : int, password : str):
    try:
        update = db.query(User).filter(User.userAppId == user_app_id).update({
            User.password:password
        })
        db.commit()
        if update == 0:
            return ErrorResponse(message="FAILED")        
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
def fcm_token_update(db:Session, user_app_id : int, fcm_token : int):
    try:
        update = db.query(User).filter(User.userAppId == user_app_id).update({
            User.fcmToken:fcm_token,
            User.tableTimestamp:func.current_timestamp()
        })
        db.commit()
        if update==0:
            return ErrorResponse(message="FAILED")
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()

def insert_user(db : Session, user_data : UserCreate):
    try:
        with db.begin():
            # Check for Existing User
            existing_user = db.query(User).filter(User.userAppId == user_data.userAppId).first()
            if existing_user:
                return ErrorResponse(message="USER ALREADY PRESENT")
            
            #Set Defaults
            dob = user_data.dob if user_data.dob else date.today()
            gender = user_data.gender if user_data.gender and user_data.gender.strip() != "" else "Male"
            joining_date = date.today()
            new_user = User(
                userAppId = user_data.userAppId,
                password = user_data.password,
                alternateNumber=user_data.alternateNumber,
                fullName=user_data.fullName,
                dob=dob,
                city=user_data.city,
                gender=gender,
                custSignUpDate=joining_date,
                emailId=user_data.emailId,
                rating=user_data.rating,
                totalNoOfReviews=user_data.totalCustomerRevies,
                alsoVendor=False,
                vendorApproved=False,
                lockApp=False,
                tableTimestamp=datetime.now()
            )

            db.add(new_user)
            db.commit()

            return ErrorResponse(message="INSERTED")

    except SQLAlchemyError as e:
        print(str(e))
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
# def login_user(db:Session, login_data : UserLogin):
#     try:
#         # Check for existing user
#         with db.begin():
#             user = db.query(
#                 User.userAppId,
#                 User.password, 
#                 User.alternateNumber,
#                 User.fullName,
#                 User.emailId,
#                 User.dob,
#                 User.city,
#                 User.gender,
#                 User.profilePicture,
#                 User.user_login_status,
#                 User.alsoVendor,
#                 User.rating,
#                 User.customerRating,
#                 User.totalNoOfReviews,
#                 User.totalCustomerReviews             
#                 ).filter(User.userAppId == login_data.userAppId).first()

#             if not user:
#                 return ErrorResponse(message="NOT REGISTERED")
            
#             # Unpack User
#             (
#                 user_app_id,
#                 stored_password,
#                 alternate_number,
#                 full_name,
#                 email_id,
#                 dob,
#                 city,
#                 gender,
#                 profile_picture,
#                 user_login_status,
#                 also_vendor,
#                 rating,
#                 customer_rating,
#                 total_vendor_rating,
#                 total_customer_rating
#             ) = user

#             # Verify password
#             if stored_password != login_data.password:
#                 return ErrorResponse(message="USERNAME OR PASSWORD WRONG")
            
#             user_dict = {
#                 "FULLNAME" : full_name,
#                 "EMAIL" : email_id,
#                 "APPID" : user_app_id,
#                 "DOB" : dob,
#                 "CITY" : city,
#                 "GENDER" : gender,
#                 "ALTERNATENUM" : alternate_number,
#                 "PROFILEPIC" : profile_picture,
#                 "VENDOR" : also_vendor,
#                 "CUSTOMERRATING" : customer_rating,
#                 "TOTALCUSTOMERRATING" : total_customer_rating
#             }

#             if also_vendor:
#                 user_dict.update({
#                     "VENDORRATING" : float(rating) if rating is not None else None,
#                     "TOTALVENDORRATING" : total_vendor_rating
#                 })
            
#             status = "LOGGEDIN"
#             message = "LOGIN SUCCESS" if user_login_status != 'LOGGEDIN' else "ALREADY_LOGGEDIN"

#             updated = db.query(User).filter(
#                 User.userAppId == login_data.userAppId,
#                 User.password == login_data.password
#             ).update({
#                 User.user_login_status : status,
#                 User.fcmToken : login_data.fcmToken,
#                 User.tableTimestamp : func.current_timestamp()
#             })

#             db.commit()

#             if updated==0:
#                 return ErrorResponse(message="LOGIN_FAILED")
            
#             return LoginResponse(message=message,user=[user_dict])

#     except SQLAlchemyError:
#         db.rollback()
#         return ErrorResponse(message="LOGIN_FAILED")
#     finally:
#         db.close()
    
def logout_user(db : Session, fcm_token : str, user_app_id : str):
    try : 
        with db.begin():
            status = "LOGGEDOUT"
            update = db.query(User).filter(User.userAppId == user_app_id).update({
                User.user_login_status : status,
                User.fcmToken : fcm_token
            })

            if update==0:
                return ErrorResponse(message="LOGOUT_FAILED")
            
            return LogoutResponse(messsage="LOGOUT_SUCCESS",status=status,userAppId=user_app_id)
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="LOGOUT FAILED")
    finally:
        db.close()
    
# def delete_user(db : Session, user_data : UserDelete):
#     try:                    
#             # Verify user credentials using login_user
#             login_result = login_user(db,UserLogin(userAppId=user_data.userAppId, password=user_data.password))
#             # print(login_result)
#             if login_result.message in ["NOT_REGISTERED","USERNAME OR PASSWORD WRONG","LOGIN FAILED"]:
#                 return ErrorResponse(message=login_result.message)
            
#             # Generate unique deleted userAppId
#             delete_base_id = f"{user_data.userAppId} DELETED"
#             unique_deleted_id = delete_base_id
#             counter = 1

#             while True:
#                 existing_user = db.query(User).filter(
#                     User.userAppId == user_data.userAppId,
#                     User.password == user_data.password
#                 ).first()

#                 if not existing_user:
#                     break

#                 unique_deleted_id = f"{delete_base_id}{counter}"
#                 counter += 1
            
#             update = db.query(User).filter(
#                 User.userAppId == user_data.userAppId,
#                 User.password == user_data.password
#             ).update({
#                 User.userAppId:unique_deleted_id,
#                 User.user_login_status:"LOGGEDOUT",
#                 User.deletionReason:user_data.deletionReason
#             })
#             db.commit()
#             if update > 0:
#                 return ErrorResponse(message="DELETED")
#             else:
#                 return ErrorResponse(message="NOT DELETED")

#     except SQLAlchemyError as e:
#         db.rollback()
#         print(str(e))
#         return ErrorResponse(message="ERROR")
#     finally:
#         db.close()

from sqlalchemy import func
from sqlalchemy.orm import Session

def delete_user(db: Session, user_data: UserDelete):
    try:
        # 1) Fetch the user (and validate password) in one go; no nested login call
        user = db.query(User).filter(User.userAppId == user_data.userAppId).first()
        if not user:
            return ErrorResponse(message="NOT_REGISTERED")
        if user.password != user_data.password:
            return ErrorResponse(message="USERNAME OR PASSWORD WRONG")

        # 2) Build a unique deleted-ID: "<orig> DELETED", "<orig> DELETED1", "<orig> DELETED2", ...
        base = f"{user_data.userAppId}.DELETED"
        unique_deleted_id = base
        counter = 1

        # Check collisions against userAppId == candidate, not the original
        while db.query(User).filter(User.userAppId == unique_deleted_id).first():
            unique_deleted_id = f"{base}{counter}"
            counter += 1
            if counter > 1000:  # safety valve
                return ErrorResponse(message="DELETE_ID_GENERATION_FAILED")
        print(unique_deleted_id)
        # 3) Update the same user row
        updated = (
            db.query(User)
            .filter(
                User.userAppId == user_data.userAppId,
                User.password == user_data.password
            )
            .update({
                # User.userAppId: unique_deleted_id,
                User.lockApp : True,
                User.user_login_status: "LOGGEDOUT",
                User.deletionReason: user_data.deletionReason,
                User.tableTimestamp: func.current_timestamp(),
            }, synchronize_session=False)
        )

        db.commit()

        if updated > 0:
            return ErrorResponse(message="DELETED")
        else:
            return ErrorResponse(message="NOT DELETED")

    except SQLAlchemyError as e:
        db.rollback()
        print(str(e))
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
def update_vendor_bank_details(db : Session, user_data : UserBankDetailsUpdate):
    try : 
        with db.begin():
            existing_user = db.query(User).filter(User.userAppId == user_data.userAppId).first()
            if not existing_user:
                return ErrorResponse(message="NOT FOUND")            
            #Collect Updata Data 
            update_data = {}
            if user_data.bankAccountHolderName and user_data.bankAccountHolderName.strip():
                update_data["bankAccountHolderName"] = user_data.bankAccountHolderName.strip()
            if user_data.bankAccountNo and user_data.bankAccountNo.strip():
                #Remove non-alphanumeric characters
                account_no = re.sub(r'[^A-Za-z0-9]', '', user_data.bankAccountNo.strip())
                if len(account_no) > 50: # Basic length validation
                    return ErrorResponse(message="ERROR_INVALID_BANKACCOUNTNO")
                update_data["bankAccountNo"] = account_no
            if user_data.bankIFSC and user_data.bankIFSC.strip():
                # Uppercase and validate IFSC (11 characters, e.g., SBIN0001234)
                ifsc = user_data.bankIFSC.strip().upper()
                if not re.match(r'^[A-Z]{4}0[A-Z0-9]{6}$', ifsc):
                    return NoUserResponse(message="ERROR_INVALID_BANKIFSC")
                update_data["bankIFSC"] = ifsc
            if user_data.bankName and user_data.bankName.strip():
                update_data["bankName"] = user_data.bankName

            if not update_data:
                return ErrorResponse(message="ERROR_NOTHING_TO_UPDATE")

            #UPDATE USER
            update = db.query(User).filter(User.userAppId == user_data.userAppId).update(update_data)
            db.commit()

            if update > 0:
                return ErrorResponse(message="UPDATED")
            
            return ErrorResponse(message="NO_CHANGES")

    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR_UPDATE")
    finally:
        db.close()
    
def profile_image_upload(db: Session, user_data : UserImageUpload):
    try : 
        with db.begin():
            user = db.query(User).filter(User.userAppId == user_data.userAppId).first()
            if not user:
                return ErrorResponse(message="NOT_FOUND")
            
            #Upload Image
            base_dir = os.path.join(os.path.dirname(__file__),'..','profilePicture')
            base_url = "http://43.204.100.185/bidApp/websocket-servermq/profilePicture"
            file_stem = re.sub(r'[^A-Za-z0-9_\-\.]', '', user_data.name.replace(' ', '_'))
            upload_result = upload_image(user_data.image, base_dir, file_stem, base_url)

            if upload_result["message"] != "UPLOADED":
                return ErrorResponse(message=upload_result["message"])
            
            #update Profile Picture

            update = db.query(User).filter(User.userAppId == user_data.userAppId).update({
                User.profilePicture : upload_result["url"],
                User.tableTimestamp : func.current_timestamp()
            })

            db.commit()

            if update > 0 :
                return ImageResponse(message="UPLOADED", url=upload_result["url"])
        return EmailErrorResponse(message="ERROR_UPDATE", error="Database update failed")


    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_UPDATE", error=str(e))
    finally:
        db.close()
    

    

def vendor_update(db : Session, vendor_data : VendorUpdate):    
    try:
        with db.begin():
            
            #Sanitize Inputs:

            user_app_id = re.sub(r'[^A-Za-z0-9_\-]', '_', vendor_data.userAppId.strip())
            car_reg_no = re.sub(r'[^A-Za-z0-9_\-]', '_', vendor_data.carregno.strip())
            car_model = vendor_data.carmodel.strip()
            model_year = vendor_data.modelyear.strip()
            owner_name = vendor_data.ownername.strip()
            also_vendor = bool(vendor_data.alsoVendor)

            #check existing user

            user = db.query(User).filter(User.userAppId == vendor_data.userAppId).first()
            if not user:
                return EmailErrorResponse(message="USER_NOT_FOUND")
            
            #Update User Table
            user.alsoVendor = also_vendor
            user.tableTimestamp = func.current_timestamp()
            db.flush() # Ensure user update is staged

            # Save registration image
            base_dir = "carDocs"
            base_url = "http://43.204.100.185/bidApp/websocket-servermq/carDocs"            
            file_stem = f"{user_app_id}_{car_reg_no}"

            image_result = upload_image(vendor_data.registration,base_dir,file_stem,base_url)
            if image_result["message"] != "UPLOADED":
                db.rollback()
                return EmailErrorResponse(message="ERROR_SAVING_FILE",error=image_result.get("error"))
            
            registration_url = image_result["url"]
            
            #Check for Duplicate Car
            existing_car = db.query(CarDetail).filter(
                CarDetail.userAppId == vendor_data.userAppId,
                CarDetail.carRegNo == vendor_data.carregno,
                CarDetail.carModel == vendor_data.carmodel,
                CarDetail.modelYear == vendor_data.modelyear,
                CarDetail.ownerName == vendor_data.ownername,
                CarDetail.registrationDoc == registration_url
            )

            if existing_car:
                # Try cleanup, but don't let cleanup failure change the API result
                try:
                    # Prefer using the actual saved filename from upload_image
                    # e.g., image_result["filename"] if you add it to upload_image’s return
                    fname = image_result.get("filename")
                    if fname:
                        os.remove(os.path.join(base_dir, fname))
                except OSError:
                    pass

                # Abort the transaction cleanly
                # Option A: raise an HTTPException (recommended for errors)
                raise EmailErrorResponse(message="ERROR_ALREADY_EXISTS")
                
            
            #Insert Car Details
            new_car = CarDetail(
                userAppId=user_app_id,
                carRegNo=car_reg_no,
                carModel=car_model,
                modelYear=model_year,
                ownerName=owner_name,
                registrationDoc=registration_url 
            )
            db.add(new_car)
            # db.commit()


            # Send email
            html_content = f"""
            <html>
            <head>
                <title>New Vendor Registration Approval Required</title>
                <style>
                    body {{ font-family: Arial, sans-serif; color: #333; }}
                    .container {{ padding: 20px; border: 1px solid #ccc; border-radius: 10px; background-color: #f9f9f9; }}
                    .highlight {{ font-weight: bold; color: #2c3e50; }}
                    .footer {{ margin-top: 20px; font-size: 12px; color: #888; }}
                </style>
            </head>
            <body>
            <div class="container">
                <h2>Vendor Approval Request</h2>
                <p>Dear Super Admin,</p>
                <p>A new vendor has just registered and requires your approval.</p>
                <p><strong>User Vendor Name:</strong> <span class="highlight">{html.escape(owner_name)}</span></p>
                <p><strong>User App ID:</strong> <span class="highlight">{html.escape(str(user_app_id))}</span></p>
                <p>Please log in to the admin panel to review and approve the vendor registration.</p>
                <p>Thank you.</p>
                <div class="footer">
                    This is an automated message. Please do not reply.
                </div>
            </div>
            </body>
            </html>
            """

            email_result = send_email(
                message=html_content,
                subject="New Vendor Registered for OpenBid - Approval Pending",
                from_address="customersupport@wizzride.com",
                from_name="WizzRide",
                to_address="ashish.mittal@wizzride.com",
                to_name="Wizzride",
                cc_address="founders@wizzride.com",
                cc_name="Wizzride"
            )

            if email_result["message"] != "SENT":
                pass

        return EmailErrorResponse(message="UPDATED")
        
    except SQLAlchemyError as e:
        db.rollback()
        try:
            os.remove(os.path.join(base_dir,f"{file_stem}.{image_result.get('extension','png')}"))
        except (OSError, NameError) as e:
            print(f"Failed to clean up file: {e}")
        print(f"SQLAlchemy Error: {str(e)}")
        return EmailErrorResponse(message="ERROR", error=str(e))
    except Exception as e:
        db.rollback()
        # Clean up saved file on general failure
        try:
            os.remove(os.path.join(base_dir, f"{file_stem}.{image_result.get('extension', 'png')}"))
        except (OSError, NameError) as e:
            print(f"Failed to clean up file: {e}")
            print(f"General Error: {str(e)}")
            return EmailErrorResponse(message="ERROR", error=str(e))            
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()

def get_all_active_vendors(db: Session):
    try:        
        vendors = db.query(User.userAppId,User.fcmToken).filter(
            User.alsoVendor==True,
            User.vendorApproved==True
        ).all()                    

        return [VendorResponse(userAppId=vendor[0],fcmToken=vendor[1]) for vendor in vendors]
    except SQLAlchemyError as e:
        return EmailErrorResponse(message="ERORR",error=str(e))
    finally:
        db.close()
    

def vendor_update_with_kyc(db:Session, vendor_update_data : VendorKycCreate):
    
    #configuration file

    BASE_LOCAL_DIR = os.path.join(os.path.dirname(__file__),'..','vendorDocuments')
    BASE_PUBLIC_URL = 'http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/'
    EMAIL_TO = 'openbidresourceteam@wizzride.com'
    EMAIL_TO_NAME = 'Wizzride'
    EMAIL_FROM = 'customersupport@wizzride.com'
    EMAIL_FROM_NAME = 'WizzRide'
    EMAIL_SUBJECT = 'New/Updated Vendor Registration - Approval Pending'

    try:
        user = db.query(User).filter(User.userAppId == vendor_update_data.userAppId).first()
        if not user:
            return EmailErrorResponse(message="ERROR_INVALID_USER_APP_ID")
        
        #Process Image 
        timestamp = datetime.now().strftime("%Y%M%D_%H%M%S")
        user_dir = os.path.join(BASE_LOCAL_DIR,vendor_update_data.userAppId)
        base_url = f"{BASE_PUBLIC_URL}{vendor_update_data.userAppId}"

        #Adhar Image
        adhar_result = upload_image(
            vendor_update_data.imageAadhar,
            base_dir=user_dir,
            file_stem=f"Aadhar_{timestamp}",
            base_url=base_url
        )
        if adhar_result["message"] != 'UPLOADED':
            return EmailErrorResponse(message=adhar_result["message"],error=adhar_result.get("error"))
        aadhar_url = adhar_result["url"]
        
        #Pan Card Image
        pan_result = upload_image(
            vendor_update_data.imagePAN,
            base_dir=user_dir,
            file_stem=f"PAN_{timestamp}",
            base_url=base_url
        )
        if pan_result["message"] != 'UPLOADED':
            return EmailErrorResponse(message=pan_result["message"],error=pan_result.get("error"))
        pan_url = pan_result["url"]

        #Bank Passbook Image
        bank_result = upload_image(
            vendor_update_data.imageBankAccount,
            base_dir=user_dir,
            file_stem=f"Bank_{timestamp}",
            base_url=base_url
        )
        if bank_result["message"] != 'UPLOADED':
            return EmailErrorResponse(message=bank_result["message"],error=bank_result.get("error"))
        bank_url = bank_result["url"]

        joining_date = datetime.now()
        request_type = "1,2,3,4"
        address = vendor_update_data.addressLine1
        if vendor_update_data.addressLine2:
            address += vendor_update_data.addressLine2

        #UPDATE USER TABLE
        user.joiningDate = joining_date
        user.alsoVendor = vendor_update_data.alsoVendor
        user.dob = vendor_update_data.dob
        user.address = address
        user.city = vendor_update_data.city
        user.gender = vendor_update_data.gender
        user.state = vendor_update_data.state
        user.bankAccountHolderName = vendor_update_data.bankAccountHolderName
        user.bankAccountNo = vendor_update_data.bankAccountNo
        user.bankIFSC = vendor_update_data.bankIFSC
        user.bankName = vendor_update_data.bankName
        user.requestTypePreferences = request_type

        db.commit()

        submitted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html_content = f"""
            <html>
            <head>
                <title>New/Updated Vendor Registration</title>
                <style>
                    body {{ font-family: Arial, sans-serif; color: #333; }}
                    .box  {{ padding: 16px; border: 1px solid #ddd; border-radius: 10px; background: #fafafa; }}
                    .row  {{ margin-bottom: 8px; }}
                    .k    {{ font-weight: bold; color: #2c3e50; display: inline-block; width: 220px; }}
                    a     {{ color: #0b5ed7; text-decoration: none; }}
                </style>
            </head>
            <body>
                <div class="box">
                    <h2>Vendor KYC — New/Updated Submission</h2>
                    <div class="row"><span class="k">User App ID:</span> {vendor_update_data.userAppId}</div>
                    <div class="row"><span class="k">Also Vendor:</span> {'Yes' if vendor_update_data.alsoVendor else 'No'}</div>
                    <hr>                
                    <div class="row"><span class="k">DOB:</span> {vendor_update_data.dob}</div>                
                    <div class="row"><span class="k">GENDER:</span> {vendor_update_data.gender}</div>                
                    <div class="row"><span class="k">Address Line 1:</span> {vendor_update_data.addressLine1}</div>
                    <div class="row"><span class="k">Address Line 2:</span> {vendor_update_data.addressLine2 or ''}</div>
                    <div class="row"><span class="k">City / State:</span> {vendor_update_data.city} / {vendor_update_data.state}</div>
                    <hr>
                    <div class="row"><span class="k">Bank A/C Holder:</span> {vendor_update_data.bankAccountHolderName}</div>
                    <div class="row"><span class="k">Bank A/C No:</span> {vendor_update_data.bankAccountNo}</div>
                    <div class="row"><span class="k">IFSC:</span> {vendor_update_data.bankIFSC}</div>
                    <div class="row"><span class="k">Bank Name:</span> {vendor_update_data.bankName}</div>
                    <hr>
                    <div class="row"><span class="k">Aadhaar Doc:</span> <a href="{aadhar_url}" target="_blank">{aadhar_url}</a></div>
                    <div class="row"><span class="k">PAN Doc:</span> <a href="{pan_url}" target="_blank">{pan_url}</a></div>
                    <div class="row"><span class="k">Bank Doc:</span> <a href="{bank_url}" target="_blank">{bank_url}</a></div>
                    <hr>
                    <div class="row">Submitted at: {submitted_at}</div>
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
        except Exception as e:
            return EmailErrorResponse(message="ERROR_MAIL_SENT",error=str(e))        
        
        return EmailErrorResponse(message="UPDATED")
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR")
    finally:
        db.close()
    

def update_request_type_selections(db : Session, data : UpdateRequestTypeSelectionsRequest):
    """
    Update user's requestTypePreferences (CSV of RTDIDs).
    Matches PHP updateRequestTypeSelections() 1:1.
    """

    try:
        # (1) Fetch current value
        user = db.query(User).filter(User.userAppId== data.userAppId).first()
        if not user:
            return EmailErrorResponse(message="NOT_FOUND")
        
        curr_csv = user.requestTypePreferences or ""
        curr_ids = _ids_to_csv(curr_csv)

        # (2) If not provided → nothing to do
        if data.requestTypeIds is None : 
            return EmailErrorResponse(message="NOTHING_TO_UDPATE")        
        new_ids = _to_id_array(data.requestTypeIds)

        # (3) Optional validation
        if data.validate and new_ids:
            valid_ids = db.query(RequestType.RTDID).filter(
                RequestType.RTDID.in_(new_ids)
            ).all()
            valid_set = {row[0] for row in valid_ids}
            if len(valid_set) != len(new_ids):
                return EmailErrorResponse(message="ERROR_INVALID_REQUESTTYPE")
        
        # (4) Build new CSV        
        next_csv = _ids_to_csv(new_ids)

        # (5) Short-circuit if unchanged
        if next_csv == curr_csv:
            return EmailErrorResponse(message="NOTHING_TO_UPDATE")
        
        # (6) Update DB
        user.requestTypePreferences = next_csv
        db.commit()

        return EmailErrorResponse(message="UPDATED")

    except SQLAlchemyError as e: 
        db.rollback()
        return EmailErrorResponse(message="ERROR_UPDATE",error=str(e))
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    

def update_region_city_selections(db: Session, data : UpdateRegionCitySelectionsRequest):
    """
    Update user's regionPreferences and cityPreferences (CSV of IDs).
    Matches PHP updateRegionCitySelections() 1:1.
    """
    try:
        # (1) Fetch current values
        user = db.query(User).filter(User.userAppId == data.userAppId).first()
        if not user:
            return EmailErrorResponse(message="NOT_FOUND")
        
        curr_region_csv = user.regionPreferences or ""
        curr_city_csv = user.cityPreferences or ""
        curr_region_ids = _to_id_array(curr_region_csv)
        curr_city_ids = _to_id_array(curr_city_csv)

        # (2) Determine if fields were provided
        regions_provided = data.regionIds is not None
        city_provided = data.cityIds is not None

        if not regions_provided or not city_provided:
            return EmailErrorResponse(message="NOTHING_TO_UPDATE")
        
        #(3) Parse New Values
        new_region_ids = _to_id_array(data.regionIds) if regions_provided else curr_region_ids
        new_city_ids = _to_id_array(data.cityIds) if city_provided else curr_city_ids

        # (4) Optional validation
        if data.validate:
            if regions_provided and new_region_ids:
                if not new_region_ids:
                    return None
            valid = db.query(Region.RDID).filter(Region.RDID.in_(new_region_ids)).all()
            valid_set = {row[0] for row in valid}
            if len(valid_set) != len(new_region_ids):
                return EmailErrorResponse(message="ERROR_INVALID_REGIONIDS")            
        
            if city_provided and new_city_ids:
                if not new_city_ids:
                    return None
            valid = db.query(LocationDetail.LID).filter(LocationDetail.LID.in_(new_city_ids)).all()
            valid_set = {row[0] for row in valid}
            if len(valid_set) != len(new_city_ids):
                return EmailErrorResponse(message="ERROR_INVALID_CITYIDS")
            return None
        
        # (5) Build new CSVs
        next_region_csv = _ids_to_csv(new_region_ids)
        next_city_csv = _ids_to_csv(new_city_ids)

        # (6) Short-circuit if unchanged
        if next_region_csv == curr_region_csv and next_city_csv == curr_city_csv:
            return EmailErrorResponse(message="NOTHING_TO_UDPATE")
        
        # (7) Update DB
        user.regionPreferences = next_region_csv
        user.cityPreferences = next_city_csv
        db.commit()

        return EmailErrorResponse(message="UPDATED")
    
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_UPDATE")
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()

def get_request_type_selections(db:Session, user_app_id : str):
    """
    Get user's request type selections.
    Matches PHP getRequestTypeSelections() 1:1.
    """

    try:
        # (1) Get user preferences
        user = db.query(User).filter(User.userAppId == user_app_id).first()
        if not user:
            return EmailErrorResponse(message="NOT_FOUND")
        req_type_csv = user.requestTypePreferences or ""
        selected_ids = _csv_to_set(req_type_csv)

        # (2) Load all request types
        types_raw = db.query(
            RequestType.RTDID,
            RequestType.requestType
        ).order_by(
            RequestType.requestType.asc()
        ).all()

        # (3) Build response
        types = []
        for rtdid,rtype in types_raw:
            types.append({
                "REQUEST_TYPE_ID": rtdid,
                "REQUEST_TYPE_NAME": rtype or "",
                "SELECTED": rtdid in selected_ids
            })

        # (4) Sort by name (case-insensitive) — already ordered, but ensure stable
        types.sort(key=lambda x: x["REQUEST_TYPE_NAME"].lower())

        # (5) Convert to Pydantic models
        # response_data = []

        return [RequestTypeResponse(**t) for t in types]

    except SQLAlchemyError as e:
        print(str(e))
        return EmailErrorResponse(message="ERROR_PREPARE")
    except Exception as e:
        print(str(e))
        return EmailErrorResponse(message="ERROR_PREPARE")
