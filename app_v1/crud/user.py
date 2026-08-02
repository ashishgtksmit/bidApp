import base64
import binascii
from zoneinfo import ZoneInfo

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
from ..models.admin_number import AdminNumber
from ..schemas.user_table import (NoUserResponse,BidderDetail, RejectUserRequest,UserBankDetailsResponse,
                                  LogoutResponse,UserDelete,UserBankDetailsUpdate,
                                  UserImageUpload,VendorUpdate,VendorResponse,VendorKycCreate, 
                                  UpdateRequestTypeSelectionsRequest,UpdateRegionCitySelectionsRequest,
                                  RequestTypeResponse,GetUserDetailsResponse,CustomerListItem,AdminNumberResponse,
                                  UpdateVendorApprovalRequest,UpdateVendorLockAppStatusRequest,UploadVendorDocumentRequest,
                                  UploadVendorDocumentResponse)
from ..utils.common import ErrorResponse,ImageResponse,EmailErrorResponse,_ids_to_csv,_to_id_array,_csv_to_set
from ..utils.image import upload_image,upload_vendor_profile_picture_azure,azure_blob_upload,azure_blob_delete_by_url
from ..utils.email import send_email
from ..utils.fcm import subscribe_token_to_topics, TOPIC_ALL_USERS, TOPIC_ALL_VENDORS, unsubscribe_token_from_topics
from ..services.vendor_filtering import get_all_vendors_enriched
from datetime import date, datetime
from typing import Optional
from fastapi import HTTPException, status
import re
import os
import html
from ..models.request_table import Request

def _vendor_rating_float(rating) -> float | None:
    if rating is None:
        return None
    try:
        return float(rating)
    except (TypeError, ValueError):
        return None


def _to_get_user_details_response(user: User, *, include_timestamp: bool = False) -> GetUserDetailsResponse:
    """Map a User ORM row to the authenticated session profile contract (PR6)."""
    also_vendor = bool(getattr(user, "alsoVendor", False))
    vendor_rating = _vendor_rating_float(user.rating) if also_vendor else None
    total_vendor_rating = user.totalNoOfReviews if also_vendor else None

    payload = dict(
        USERAPPID=user.userAppId,
        ALTERNATEMNUM=user.alternateNumber or "",
        FULLNAME=user.fullName,
        EMAILID=user.emailId,
        EMAIL=user.emailId,
        DOB=user.dob,
        CITY=user.city,
        GENDER=user.gender,
        PROFILEPIC=user.profilePicture,
        # Legacy fields: vendor rating columns (do not use as customer rating).
        RATING=_vendor_rating_float(user.rating),
        TOTALREVIEWS=user.totalNoOfReviews,
        CUSTOMERRATING=(
            str(user.customerRating)
            if user.customerRating is not None
            else None
        ),
        TOTALCUSTOMERRATING=user.totalCustomerReviews,
        VENDORRATING=vendor_rating,
        TOTALVENDORRATING=total_vendor_rating,
        FCMTOKEN=user.fcmToken,
        USERLOGINSTATUS=user.user_login_status,
        ALSOVENDOR=also_vendor,
        VENDOR=also_vendor,
    )
    if include_timestamp:
        payload["TABLETIMESTAMP"] = user.tableTimestamp
    return GetUserDetailsResponse(**payload)


def get_users_all(db:Session):
    try:
        with db.begin():
            users = db.query(User).all()
            if not users:
                return NoUserResponse(message="NO_USER")           

            return [
                _to_get_user_details_response(user, include_timestamp=True)
                for user in users
            ]
    except SQLAlchemyError as e : 
        db.rollback()
        return EmailErrorResponse(message="ERROR_",error=str(e))
    finally:
        db.close()
    
def get_user_details(db: Session, userAppId : str):
    try:
        users = db.query(User).filter(User.userAppId == str(userAppId).strip()).all()

        if not users:
            return NoUserResponse(message="NO REGISTERED")
        
        return [_to_get_user_details_response(user) for user in users]
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


# def get_all_vendors(db:Session):
#     try:
#         vendors = db.query(User).filter(
#             (User.alsoVendor == 1) & (User.vendorApproved == 1)
#         ).all()

#         if not vendors:
#             return NoUserResponse(message="NO VENDORS FOUND")
        
#         return [GetUserDetailsResponse(
#             USERAPPID=vendor.userAppId,
#             ALTERNATEMNUM=vendor.alternateNumber,
#             FULLNAME=vendor.fullName,
#             EMAILID=vendor.emailId,
#             DOB=vendor.dob,
#             CITY=vendor.city,
#             GENDER=vendor.gender,
#             PROFILEPIC=vendor.profilePicture,
#             RATING=vendor.rating,
#             TOTALREVIEWS=vendor.totalNoOfReviews,
#             FCMTOKEN=vendor.fcmToken,
#             USERLOGINSTATUS=vendor.user_login_status,
#             ALSOVENDOR=vendor.alsoVendor,
#             TABLETIMESTAMP=vendor.tableTimestamp

#         ) for vendor in vendors]
#     except SQLAlchemyError:
#         return ErrorResponse(message="ERROR_PREPARE")
#     finally:
#         db.close()

def get_all_vendors(db: Session):
    try:
        return get_all_vendors_enriched(db, approved_only=True)
    except SQLAlchemyError as e:
        return EmailErrorResponse(message="ERROR_PREPARE", error=str(e))

# def get_vendor_by_rid(db: Session, rid: int):    
#     try : 
#         vendors = db.query(
#             User.fullName,
#             User.userAppId,
#             User.alternateNumber,
#             User.emailId,
#             User.dob,
#             User.city,
#             User.rating,
#             User.totalNoOfReviews,
#             User.profilePicture,   # ← missing comma fixed
#             BidDetail.bidderID,
#             BidDetail.bidAmount,
#             User.tags,
#             User.noOfTripsCompleted,
#             BidDetail.CARID,

#             CarDetail.userAppId,
#             CarDetail.carRegNo,
#             CarDetail.carModel,
#             CarDetail.modelYear,
#             CarDetail.carColor,
#             CarDetail.ownerName,
#             CarDetail.registrationDoc,
#             CarDetail.powerOfAttorneyDoc,
#             CarDetail.registeredOn,
#             CarDetail.adminApproved,
#             CarDetail.carOwnedBySameVendor,
#             CarDetail.CTD,
#             CarDetail.imageVehicleFront,
#             CarDetail.imageVehicleSide,

#             CarTypeDetail.car_type,
#             CarTypeDetail.car_sub_type,
#             CarTypeDetail.capacity,
#             CarTypeDetail.image_url
#         ).join(
#             User, User.userAppId == BidDetail.bidderID
#         ).outerjoin(
#             CarDetail, CarDetail.CARID == BidDetail.CARID
#         ).outerjoin(
#             CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
#         ).filter(
#             (BidDetail.rID == rid) &
#             (BidDetail.bidStatus == 'REQUEST - CONFIRMED')
#         ).all()

#         if not vendors:
#             return NoUserResponse(message="NO VENDOR DATA FOUND")
        
#         result = []
#         for(full_name, primary_number, alternate_number, email_id, dob, city,rating, total_no_of_reviews, profile_pic, 
#             bidder_id, bid_amount, tags_str, no_of_trips_completed, car_id, user_app_id, car_reg_no, car_model,
#             model_year, car_color, owner_name, registration_doc, power_of_attorney_doc, registered_on, admin_approved,
#             car_owned_by_same_vendor, ctd, image_vehicle_front, image_vehicle_side, car_type, car_sub_type, 
#             capacity, image_url) in vendors : 

#             tag_ids = []   # start with an empty list
#             if tags_str:   # check if tags_str is not None or empty
#                 # split string by "," -> gives list like ["1", "2", "3"]
#                 tag_parts = tags_str.split(",")

#                 # go through each piece
#                 for t in tag_parts:
#                     cleaned = t.strip()   # remove spaces
#                     if cleaned:          # if not empty string
#                         tag_ids.append(int(cleaned))   # convert to int and add to list
#             else:
#                 tag_ids = []

#             #get tag names

#             tag_names = []

#             if tag_ids:
#                 tags_rows = db.query(Tag.tagsName).filter(
#                     Tag.TAGID.in_(tag_ids)
#                 ).all()

#                 for r in tags_rows:
#                     tag_names.append(r[0])

#         result.append(
#             BidderDetail(
#                 FULLNAME=full_name,
#                 PRIMARYNUMBER=primary_number,
#                 ALTERNATENUMBER=alternate_number,
#                 EMAILID=email_id,
#                 DOB=dob,
#                 CITY=city,
#                 RATING=rating,
#                 TOTALNOOFREVIEWS=total_no_of_reviews,
#                 BIDDERID=bidder_id,
#                 BIDDERAMOUT=bid_amount,
#                 PROFILEPIC=profile_pic,
#                 TAGS=tag_names,
#                 NOOFTRIPSCOMPLETED=no_of_trips_completed,
#                 CARID=car_id,
#                 CARREGNO=car_reg_no,
#                 CARMODEL=car_model,
#                 MODELYEAR=model_year,
#                 CARCOLOR=car_color,
#                 OWNERNAME=owner_name,
#                 REGISTRATIONDOC=registration_doc,
#                 POWEROFATTORNEYDOC=power_of_attorney_doc,
#                 REGISTEREDON=registered_on,
#                 ADMINAPPROVED=admin_approved,
#                 CAROWNEDBYSAMEVENDOR=car_owned_by_same_vendor,
#                 CTD=ctd,
#                 IMAGEVEHICLEFRONT=image_vehicle_front,
#                 IMAGEVEHICLESIDE=image_vehicle_side,
#                 CAR_USERAPPID=user_app_id,
#                 CAR_TYPE=car_type,
#                 CAR_SUB_TYPE=car_sub_type,
#                 CAPACITY=capacity,
#                 CAR_TYPE_IMAGE_URL=image_url
#             ))
        
#         return result
#     except SQLAlchemyError:
#         return NoUserResponse(message="ERROR_PREPARE")
#     finally:
#         db.close()


def get_vendor_by_rid(
    db: Session,
    rid: int,
    user_id: Optional[str] = None,
):
    """
    Customer-safe selected vendor details for a request (PR12).

    Ownership: JWT sub must own the request.
    Relation: request.requestWonBy + selected bid with REQUEST - CONFIRMED.
    Empty relation → []. Does not expose FCM, KYC, registration, or POA docs.
    """
    from ..schemas.request_table import CustomerBookingVendorDetail
    from ..models.tags_table import Tag

    try:
        request_row = db.query(Request).filter(Request.RID == rid).first()

        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if user_id is not None and request_row.customerAppId != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view vendor details for this request",
            )

        won_by = (
            str(request_row.requestWonBy).strip()
            if request_row.requestWonBy
            else None
        )
        if not won_by:
            return []

        try:
            won_by_key = int(won_by)
        except (TypeError, ValueError):
            won_by_key = won_by

        vendors = (
            db.query(
                User.fullName,
                User.userAppId,
                User.dob,
                User.city,
                User.gender,
                User.rating,
                User.totalNoOfReviews,
                User.joiningDate,
                User.profilePicture,
                User.tags,
                User.noOfTripsCompleted,
                BidDetail.CARID,
                CarDetail.carRegNo,
                CarDetail.carModel,
                CarDetail.modelYear,
                CarDetail.imageVehicleFront,
                CarDetail.imageVehicleSide,
                CarTypeDetail.car_type,
            )
            .join(User, User.userAppId == BidDetail.bidderID)
            .outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID)
            .outerjoin(CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD)
            .filter(
                BidDetail.rID == rid,
                BidDetail.bidderID == won_by_key,
                BidDetail.bidStatus == "REQUEST - CONFIRMED",
            )
            .all()
        )

        if not vendors:
            return []

        result = []
        for (
            full_name,
            primary_number,
            dob,
            city,
            gender,
            rating,
            total_no_of_reviews,
            joining_date,
            profile_pic,
            tags_str,
            no_of_trips_completed,
            car_id,
            car_reg_no,
            car_model,
            model_year,
            image_vehicle_front,
            image_vehicle_side,
            car_type,
        ) in vendors:
            tag_ids = []
            if tags_str:
                for t in str(tags_str).split(","):
                    cleaned = t.strip()
                    if cleaned.isdigit():
                        tag_ids.append(int(cleaned))

            tag_names = []
            if tag_ids:
                tag_rows = db.query(Tag.tagsName).filter(Tag.TAGID.in_(tag_ids)).all()
                tag_names = [row[0] for row in tag_rows]

            result.append(
                CustomerBookingVendorDetail(
                    FULLNAME=full_name,
                    PRIMARYNUMBER=str(primary_number) if primary_number is not None else None,
                    DOB=dob,
                    CITY=city,
                    GENDER=gender,
                    RATING=rating,
                    TOTALNOOFREVIEWS=total_no_of_reviews,
                    JOININGDATE=joining_date,
                    PROFILEPIC=profile_pic,
                    TAGS=tag_names,
                    NOOFTRIPSCOMPLETED=no_of_trips_completed,
                    CARID=car_id,
                    CARREGNO=car_reg_no,
                    CARMODEL=car_model,
                    MODELYEAR=model_year,
                    IMAGEVEHICLEFRONT=image_vehicle_front,
                    IMAGEVEHICLESIDE=image_vehicle_side,
                    CAR_TYPE=car_type,
                )
            )

        return result

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        print(f"[get_vendor_by_rid] ERROR: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to load vendor details",
        ) from None


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
    
    
# def fcm_token_update(db:Session, user_app_id : int, fcm_token : int):
#     try:
#         update = db.query(User).filter(User.userAppId == user_app_id).update({
#             User.fcmToken:fcm_token,
#             User.tableTimestamp:func.current_timestamp()
#         })
#         db.commit()
#         if update==0:
#             return ErrorResponse(message="FAILED")
#         return ErrorResponse(message="UPDATED")
#     except SQLAlchemyError:
#         db.rollback()
#         return ErrorResponse(message="ERROR")
#     finally:
#         db.close()

def fcm_token_update(db: Session, user_app_id: str, fcm_token: str):
    try:
        user = (
            db.query(User)
            .filter(User.userAppId == str(user_app_id).strip())
            .first()
        )

        if not user:
            return EmailErrorResponse(message="FAILED")

        cleaned_token = str(fcm_token).strip() if fcm_token is not None else ""
        if not cleaned_token or cleaned_token.lower() in {"null", "none", "na"}:
            return EmailErrorResponse(message="ERROR", error="INVALID_FCM_TOKEN")

        user.fcmToken = cleaned_token
        user.tableTimestamp = func.current_timestamp()

        db.commit()
        db.refresh(user)

        topics = [TOPIC_ALL_USERS]
        if bool(getattr(user, "alsoVendor", False)):
            topics.append(TOPIC_ALL_VENDORS)

        topic_result = subscribe_token_to_topics(cleaned_token, topics)

        if not topic_result.get("success"):
            return EmailErrorResponse(
                message="UPDATED",
                error=f"TOPIC_SUBSCRIPTION_PARTIAL: {topic_result}"
            )

        return EmailErrorResponse(message="UPDATED")

    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR", error=str(e))
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR", error=str(e))

    
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

def logout_user(db: Session, user_app_id: str):
    try:
        user = db.query(User).filter(User.userAppId == user_app_id).first()
        if not user:
            return ErrorResponse(message="LOGOUT_FAILED")

        old_token = (user.fcmToken or "").strip()
        topics = [TOPIC_ALL_USERS]

        if bool(getattr(user, "alsoVendor", False)):
            topics.append(TOPIC_ALL_VENDORS)

        status = "LOGGEDOUT"
        user.user_login_status = status
        user.fcmToken = None

        db.commit()

        if old_token and old_token.lower() not in {"null", "none", "na"}:
            try:
                unsubscribe_token_from_topics(old_token, topics)
            except Exception:
                pass

        return LogoutResponse(
            message="LOGOUT_SUCCESS",
            status=status,
            userAppId=user_app_id,
        )

    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="LOGOUT_FAILED", error=str(e))
    
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
                User.userAppId: unique_deleted_id,
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
    
# def profile_image_upload(db: Session, user_data : UserImageUpload):
#     try : 
#         with db.begin():
#             user = db.query(User).filter(User.userAppId == user_data.userAppId).first()
#             if not user:
#                 return ErrorResponse(message="NOT_FOUND")
            
#             #Upload Image
#             base_dir = os.path.join(os.path.dirname(__file__),'..','profilePicture')
#             base_url = "http://43.204.100.185/bidApp/websocket-servermq/profilePicture"
#             file_stem = re.sub(r'[^A-Za-z0-9_\-\.]', '', user_data.name.replace(' ', '_'))
#             upload_result = upload_image(user_data.image, base_dir, file_stem, base_url)

#             if upload_result["message"] != "UPLOADED":
#                 return ErrorResponse(message=upload_result["message"])
            
#             #update Profile Picture

#             update = db.query(User).filter(User.userAppId == user_data.userAppId).update({
#                 User.profilePicture : upload_result["url"],
#                 User.tableTimestamp : func.current_timestamp()
#             })

#             db.commit()

#             if update > 0 :
#                 return ImageResponse(message="UPLOADED", url=upload_result["url"])
#         return EmailErrorResponse(message="ERROR_UPDATE", error="Database update failed")


#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_UPDATE", error=str(e))
#     finally:
#         db.close()


def profile_image_upload(db: Session, user_data):
    """
    PHP-equivalent behavior of imageUpload():
    - requires image and userAppId
    - uploads to Azure using stable filename {APPID}_profile.jpg
    - updates userTable.profilePicture
    """

    try:
        image = getattr(user_data, "image", None)
        app_id = getattr(user_data, "userAppId", None)

        if not image or not app_id:
            return ErrorResponse(message="INVALID_INPUT")

        image_str = str(image).strip()
        app_id = str(app_id).strip()

        # Optional header stripping like PHP
        if re.match(r"^data:image/\w+;base64,", image_str, flags=re.IGNORECASE):
            image_str = re.sub(r"^data:image/\w+;base64,", "", image_str, flags=re.IGNORECASE)

        # Base64 validation
        try:
            binary = base64.b64decode(image_str, validate=True)
        except (binascii.Error, ValueError):
            return ErrorResponse(message="INVALID_IMAGE")

        if not binary:
            return ErrorResponse(message="INVALID_IMAGE")

        # Rebuild data URI for azure helper if needed
        # PHP always stores as .jpg with stable name
        # We keep stable naming parity here.
        normalized_base64 = "data:image/jpeg;base64," + base64.b64encode(binary).decode("utf-8")

        blob_name = f"{app_id}_profile"

        ok, file_url = azure_blob_upload(
            blob_name=blob_name,
            base64_data=normalized_base64,
            make_public=True,   # profile pictures should be publicly accessible like PHP URL usage
            container_type="profile" if "container_type" in azure_blob_upload.__code__.co_varnames else None
        )

        if not ok:
            # try to mimic PHP style if helper returned code-like error
            return ErrorResponse(message=str(file_url or "AZURE_UPLOAD_FAILED"))

        # Add cache-buster like PHP ?v=time()
        # only if helper returned clean URL without query
        if "?v=" not in str(file_url):
            import time
            separator = "&" if "?" in str(file_url) else "?"
            file_url = f"{file_url}{separator}v={int(time.time())}"

        updated = (
            db.query(User)
            .filter(User.userAppId == app_id)
            .update(
                {User.profilePicture: file_url},
                synchronize_session=False
            )
        )

        if updated == 0:
            db.rollback()
            return ErrorResponse(message="DB_UPDATE_FAILED")

        db.commit()
        return ImageResponse(message="UPLOADED", url=file_url)

    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="DB_ERROR")

    except Exception as e:
        db.rollback()
        return ErrorResponse(message=str(e))
    

    

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
    

# def vendor_update_with_kyc(db:Session, vendor_update_data : VendorKycCreate):
    
#     #configuration file

#     BASE_LOCAL_DIR = os.path.join(os.path.dirname(__file__),'..','vendorDocuments')
#     BASE_PUBLIC_URL = 'http://43.204.100.185/bidApp/websocket-servermq/vendorDocuments/'
#     EMAIL_TO = 'openbidresourceteam@wizzride.com'
#     EMAIL_TO_NAME = 'Wizzride'
#     EMAIL_FROM = 'customersupport@wizzride.com'
#     EMAIL_FROM_NAME = 'WizzRide'
#     EMAIL_SUBJECT = 'New/Updated Vendor Registration - Approval Pending'

#     try:
#         user = db.query(User).filter(User.userAppId == vendor_update_data.userAppId).first()
#         if not user:
#             return EmailErrorResponse(message="ERROR_INVALID_USER_APP_ID")
        
#         #Process Image 
#         timestamp = datetime.now().strftime("%Y%M%D_%H%M%S")
#         user_dir = os.path.join(BASE_LOCAL_DIR,vendor_update_data.userAppId)
#         base_url = f"{BASE_PUBLIC_URL}{vendor_update_data.userAppId}"

#         #Adhar Image
#         adhar_result = upload_image(
#             vendor_update_data.imageAadhar,
#             base_dir=user_dir,
#             file_stem=f"Aadhar_{timestamp}",
#             base_url=base_url
#         )
#         if adhar_result["message"] != 'UPLOADED':
#             return EmailErrorResponse(message=adhar_result["message"],error=adhar_result.get("error"))
#         aadhar_url = adhar_result["url"]
        
#         #Pan Card Image
#         pan_result = upload_image(
#             vendor_update_data.imagePAN,
#             base_dir=user_dir,
#             file_stem=f"PAN_{timestamp}",
#             base_url=base_url
#         )
#         if pan_result["message"] != 'UPLOADED':
#             return EmailErrorResponse(message=pan_result["message"],error=pan_result.get("error"))
#         pan_url = pan_result["url"]

#         #Bank Passbook Image
#         bank_result = upload_image(
#             vendor_update_data.imageBankAccount,
#             base_dir=user_dir,
#             file_stem=f"Bank_{timestamp}",
#             base_url=base_url
#         )
#         if bank_result["message"] != 'UPLOADED':
#             return EmailErrorResponse(message=bank_result["message"],error=bank_result.get("error"))
#         bank_url = bank_result["url"]

#         joining_date = datetime.now()
#         request_type = "1,2,3,4"
#         address = vendor_update_data.addressLine1
#         if vendor_update_data.addressLine2:
#             address += vendor_update_data.addressLine2

#         #UPDATE USER TABLE
#         user.joiningDate = joining_date
#         user.alsoVendor = vendor_update_data.alsoVendor
#         user.dob = vendor_update_data.dob
#         user.address = address
#         user.city = vendor_update_data.city
#         user.gender = vendor_update_data.gender
#         user.state = vendor_update_data.state
#         user.bankAccountHolderName = vendor_update_data.bankAccountHolderName
#         user.bankAccountNo = vendor_update_data.bankAccountNo
#         user.bankIFSC = vendor_update_data.bankIFSC
#         user.bankName = vendor_update_data.bankName
#         user.requestTypePreferences = request_type

#         db.commit()

#         submitted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
#         html_content = f"""
#             <html>
#             <head>
#                 <title>New/Updated Vendor Registration</title>
#                 <style>
#                     body {{ font-family: Arial, sans-serif; color: #333; }}
#                     .box  {{ padding: 16px; border: 1px solid #ddd; border-radius: 10px; background: #fafafa; }}
#                     .row  {{ margin-bottom: 8px; }}
#                     .k    {{ font-weight: bold; color: #2c3e50; display: inline-block; width: 220px; }}
#                     a     {{ color: #0b5ed7; text-decoration: none; }}
#                 </style>
#             </head>
#             <body>
#                 <div class="box">
#                     <h2>Vendor KYC — New/Updated Submission</h2>
#                     <div class="row"><span class="k">User App ID:</span> {vendor_update_data.userAppId}</div>
#                     <div class="row"><span class="k">Also Vendor:</span> {'Yes' if vendor_update_data.alsoVendor else 'No'}</div>
#                     <hr>                
#                     <div class="row"><span class="k">DOB:</span> {vendor_update_data.dob}</div>                
#                     <div class="row"><span class="k">GENDER:</span> {vendor_update_data.gender}</div>                
#                     <div class="row"><span class="k">Address Line 1:</span> {vendor_update_data.addressLine1}</div>
#                     <div class="row"><span class="k">Address Line 2:</span> {vendor_update_data.addressLine2 or ''}</div>
#                     <div class="row"><span class="k">City / State:</span> {vendor_update_data.city} / {vendor_update_data.state}</div>
#                     <hr>
#                     <div class="row"><span class="k">Bank A/C Holder:</span> {vendor_update_data.bankAccountHolderName}</div>
#                     <div class="row"><span class="k">Bank A/C No:</span> {vendor_update_data.bankAccountNo}</div>
#                     <div class="row"><span class="k">IFSC:</span> {vendor_update_data.bankIFSC}</div>
#                     <div class="row"><span class="k">Bank Name:</span> {vendor_update_data.bankName}</div>
#                     <hr>
#                     <div class="row"><span class="k">Aadhaar Doc:</span> <a href="{aadhar_url}" target="_blank">{aadhar_url}</a></div>
#                     <div class="row"><span class="k">PAN Doc:</span> <a href="{pan_url}" target="_blank">{pan_url}</a></div>
#                     <div class="row"><span class="k">Bank Doc:</span> <a href="{bank_url}" target="_blank">{bank_url}</a></div>
#                     <hr>
#                     <div class="row">Submitted at: {submitted_at}</div>
#                 </div>
#             </body>
#             </html>
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
#         except Exception as e:
#             return EmailErrorResponse(message="ERROR_MAIL_SENT",error=str(e))        
        
#         return EmailErrorResponse(message="UPDATED")
#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR")
#     finally:
#         db.close()


def vendor_update_with_kyc(db: Session, vendor_update_data: VendorKycCreate):
    EMAIL_TO = "openbidresourceteam@wizzride.com"
    EMAIL_TO_NAME = "Wizzride"
    EMAIL_FROM = "ticketdetails@wizzride.com"
    EMAIL_FROM_NAME = "WizzRide"
    EMAIL_SUBJECT = "New/Updated Vendor Registration - Approval Pending"

    def clean(v) -> str:
        return str(v or "").strip()

    tz = ZoneInfo("Asia/Kolkata")

    # ----------------------------------
    # REQUIRED FIELDS (same as PHP)
    # ----------------------------------
    required_fields = {
        "userAppId": getattr(vendor_update_data, "userAppId", None),
        "alsoVendor": getattr(vendor_update_data, "alsoVendor", None),
        "dob": getattr(vendor_update_data, "dob", None),
        "gender": getattr(vendor_update_data, "gender", None),
        "addressLine1": getattr(vendor_update_data, "addressLine1", None),
        "city": getattr(vendor_update_data, "city", None),
        "state": getattr(vendor_update_data, "state", None),
        "bankAccountHolderName": getattr(vendor_update_data, "bankAccountHolderName", None),
        "bankAccountNo": getattr(vendor_update_data, "bankAccountNo", None),
        "bankIFSC": getattr(vendor_update_data, "bankIFSC", None),
        "bankName": getattr(vendor_update_data, "bankName", None),
        "imageAadhar": getattr(vendor_update_data, "imageAadhar", None),
        "imagePAN": getattr(vendor_update_data, "imagePAN", None),
        "imageBankAccount": getattr(vendor_update_data, "imageBankAccount", None),
    }

    for field_name, field_value in required_fields.items():
        if field_value is None or clean(field_value) == "":
            return EmailErrorResponse(message=f"ERROR_MISSING_{field_name.upper()}")
        
    # ----------------------------------
    # CLEAN INPUT (same as PHP)
    # ----------------------------------
    user_app_id = clean(vendor_update_data.userAppId).replace(" ", "")
    also_vendor = 1 if (
        vendor_update_data.alsoVendor == 1
        or vendor_update_data.alsoVendor == "1"
        or vendor_update_data.alsoVendor is True
    ) else 0

    # DOB validation like PHP
    dob_raw = vendor_update_data.dob
    dob_sql = None
    try:
        if hasattr(dob_raw, "strftime"):
            dob_sql = dob_raw.strftime("%Y-%m-%d")
        else:
            # accept ISO-like string first
            dob_sql = datetime.fromisoformat(str(dob_raw)).strftime("%Y-%m-%d")
    except Exception:
        try:
            dob_sql = datetime.strptime(str(dob_raw), "%d-%m-%Y").strftime("%Y-%m-%d")
        except Exception:
            return EmailErrorResponse(message="ERROR_INVALID_DOB")

    gender = clean(vendor_update_data.gender).upper()
    if gender not in {"M", "F", "O", "MALE", "FEMALE", "OTHER"}:
        return EmailErrorResponse(message="ERROR_INVALID_GENDER")

    address1 = clean(vendor_update_data.addressLine1)
    address2 = clean(getattr(vendor_update_data, "addressLine2", ""))
    address = f"{address1} {address2}".strip()

    city = clean(vendor_update_data.city)
    state = clean(vendor_update_data.state)

    acc_holder = clean(vendor_update_data.bankAccountHolderName)
    acc_no = clean(vendor_update_data.bankAccountNo)
    ifsc = clean(vendor_update_data.bankIFSC).upper()
    bank_name = clean(vendor_update_data.bankName)

    joining_date = datetime.now(tz).strftime("%Y-%m-%d")
    request_type_preferences = "1,2,3,4"

    new_blob_urls: list[str] = []
    old_aadhaar_url = None
    old_pan_url = None
    old_bank_url = None

    try:
        with db.begin():
            # Same PHP behavior: user must exist
            user = db.query(User).filter(User.userAppId == user_app_id).first()
            if not user:
                return EmailErrorResponse(message="USER_NOT_FOUND")

            old_aadhaar_url = user.imageAadhar
            old_pan_url = user.imagePAN
            old_bank_url = user.imageBankAccount

            ts = datetime.now(tz).strftime("%Y%m%d_%H%M%S")
            blob_base = f"{user_app_id}/"

            ok_a, aadhaar_url = azure_blob_upload(
                blob_name=f"{blob_base}Aadhaar_{ts}",
                base64_data=vendor_update_data.imageAadhar,
                make_public=False,
            )
            if not ok_a:
                raise ValueError("ERROR_SAVE_AADHAAR")
            new_blob_urls.append(aadhaar_url)

            ok_p, pan_url = azure_blob_upload(
                blob_name=f"{blob_base}PAN_{ts}",
                base64_data=vendor_update_data.imagePAN,
                make_public=False,
            )
            if not ok_p:
                raise ValueError("ERROR_SAVE_PAN")
            new_blob_urls.append(pan_url)

            ok_b, bank_url = azure_blob_upload(
                blob_name=f"{blob_base}Bank_{ts}",
                base64_data=vendor_update_data.imageBankAccount,
                make_public=False,
            )
            if not ok_b:
                raise ValueError("ERROR_SAVE_BANK")
            new_blob_urls.append(bank_url)

            # update base details
            user.joiningDate = joining_date
            user.alsoVendor = also_vendor
            user.dob = dob_sql
            user.address = address
            user.city = city
            user.gender = gender
            user.state = state
            user.bankAccountHolderName = acc_holder
            user.bankAccountNo = acc_no
            user.bankIFSC = ifsc
            user.bankName = bank_name
            user.requestTypePreferences = request_type_preferences

            # update doc urls
            user.imageAadhar = aadhaar_url
            user.imagePAN = pan_url
            user.imageBankAccount = bank_url

        # ----------------------------------
        # DELETE OLD BLOBS AFTER COMMIT
        # ----------------------------------
        for old_url in [old_aadhaar_url, old_pan_url, old_bank_url]:
            if old_url:
                try:
                    azure_blob_delete_by_url(old_url)
                except Exception:
                    pass

        # ----------------------------------
        # EXACT PHP EMAIL HTML
        # ----------------------------------
        html = f'''
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

                <div class="row"><span class="k">User App ID:</span> {user_app_id}</div>
                <div class="row"><span class="k">Also Vendor:</span> {"Yes" if also_vendor else "No"}</div>

                <hr>
                <div class="row"><span class="k">DOB:</span> {dob_sql}</div>
                <div class="row"><span class="k">Gender:</span> {gender}</div>
                <div class="row"><span class="k">Address:</span> {address}</div>
                <div class="row"><span class="k">City / State:</span> {city} / {state}</div>

                <hr>
                <div class="row"><span class="k">Bank A/C Holder:</span> {acc_holder}</div>
                <div class="row"><span class="k">Account No:</span> {acc_no}</div>
                <div class="row"><span class="k">IFSC:</span> {ifsc}</div>
                <div class="row"><span class="k">Bank Name:</span> {bank_name}</div>

                <hr>
                <div class="row"><span class="k">Aadhaar:</span> <a href="{aadhaar_url}" target="_blank">{aadhaar_url}</a></div>
                <div class="row"><span class="k">PAN:</span> <a href="{pan_url}" target="_blank">{pan_url}</a></div>
                <div class="row"><span class="k">Bank Doc:</span> <a href="{bank_url}" target="_blank">{bank_url}</a></div>

                <hr>
                <div class="row">Submitted at: {datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")}</div>
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
                to_name=EMAIL_TO_NAME,
            )
        except Exception:
            # same PHP behavior: do not fail API for email
            pass

        return EmailErrorResponse(message="UPDATED")

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
    

# def update_region_city_selections(db: Session, data : UpdateRegionCitySelectionsRequest):
#     """
#     Update user's regionPreferences and cityPreferences (CSV of IDs).
#     Matches PHP updateRegionCitySelections() 1:1.
#     """
#     try:
#         # (1) Fetch current values
#         user = db.query(User).filter(User.userAppId == data.userAppId).first()
#         if not user:
#             return EmailErrorResponse(message="NOT_FOUND")
        
#         curr_region_csv = user.regionPreferences or ""
#         curr_city_csv = user.cityPreferences or ""
#         curr_region_ids = _to_id_array(curr_region_csv)
#         curr_city_ids = _to_id_array(curr_city_csv)

#         # (2) Determine if fields were provided
#         regions_provided = data.regionIds is not None
#         city_provided = data.cityIds is not None

#         if not regions_provided and not city_provided:
#             return EmailErrorResponse(message="NOTHING_TO_UPDATE")
        
#         #(3) Parse New Values
#         new_region_ids = _to_id_array(data.regionIds) if regions_provided else curr_region_ids
#         new_city_ids = _to_id_array(data.cityIds) if city_provided else curr_city_ids

#         # (4) Optional validation
#         if data.validate:
#             if regions_provided and new_region_ids:
#                 if not new_region_ids:
#                     return None
#             valid = db.query(Region.RDID).filter(Region.RDID.in_(new_region_ids)).all()
#             valid_set = {row[0] for row in valid}
#             if len(valid_set) != len(new_region_ids):
#                 return EmailErrorResponse(message="ERROR_INVALID_REGIONIDS")            
        
#             if city_provided and new_city_ids:
#                 if not new_city_ids:
#                     return None
#             valid = db.query(LocationDetail.LID).filter(LocationDetail.LID.in_(new_city_ids)).all()
#             valid_set = {row[0] for row in valid}
#             if len(valid_set) != len(new_city_ids):
#                 return EmailErrorResponse(message="ERROR_INVALID_CITYIDS")
#             return None
        
#         # (5) Build new CSVs
#         next_region_csv = _ids_to_csv(new_region_ids)
#         next_city_csv = _ids_to_csv(new_city_ids)

#         # (6) Short-circuit if unchanged
#         if next_region_csv == curr_region_csv and next_city_csv == curr_city_csv:
#             return EmailErrorResponse(message="NOTHING_TO_UDPATE")
        
#         # (7) Update DB
#         user.regionPreferences = next_region_csv
#         user.cityPreferences = next_city_csv
#         db.commit()

#         return EmailErrorResponse(message="UPDATED")
    
#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_UPDATE")
#     except Exception as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR_PREPARE")
#     finally:
#         db.close()


def update_region_city_selections(db: Session, data: UpdateRegionCitySelectionsRequest):
    """
    Update user's regionPreferences and cityPreferences.
    PHP-equivalent behavior.
    """
    try:
        if not data.userAppId or str(data.userAppId).strip() == "":
            return EmailErrorResponse(message="ERROR_MISSING_USERAPPID")

        user_app_id = str(data.userAppId).strip()

        user = db.query(User).filter(User.userAppId == user_app_id).first()
        if not user:
            return EmailErrorResponse(message="NOT_FOUND")

        cur_region_csv = user.regionPreferences or ""
        cur_city_csv = user.cityPreferences or ""

        region_ids_provided = data.regionIds is not None
        city_ids_provided = data.cityIds is not None

        if not region_ids_provided and not city_ids_provided:
            return EmailErrorResponse(message="NOTHING_TO_UPDATE")

        new_region_ids = _to_id_array(data.regionIds) if region_ids_provided else []
        new_city_ids = _to_id_array(data.cityIds) if city_ids_provided else []

        if data.validate:
            if region_ids_provided and new_region_ids:
                valid_regions = db.query(Region.RDID).filter(Region.RDID.in_(new_region_ids)).all()
                found_region_ids = {row[0] for row in valid_regions}
                for region_id in new_region_ids:
                    if region_id not in found_region_ids:
                        return EmailErrorResponse(message="ERROR_INVALID_REGIONIDS")

            if city_ids_provided and new_city_ids:
                valid_cities = db.query(LocationDetail.LID).filter(LocationDetail.LID.in_(new_city_ids)).all()
                found_city_ids = {row[0] for row in valid_cities}
                for city_id in new_city_ids:
                    if city_id not in found_city_ids:
                        return EmailErrorResponse(message="ERROR_INVALID_CITYIDS")

        next_region_csv = _ids_to_csv(new_region_ids) if region_ids_provided else cur_region_csv
        next_city_csv = _ids_to_csv(new_city_ids) if city_ids_provided else cur_city_csv

        if next_region_csv == cur_region_csv and next_city_csv == cur_city_csv:
            return EmailErrorResponse(message="NOTHING_TO_UPDATE")

        user.regionPreferences = next_region_csv
        user.cityPreferences = next_city_csv
        db.commit()

        return EmailErrorResponse(message="UPDATED")

    except SQLAlchemyError:
        db.rollback()
        return EmailErrorResponse(message="ERROR_UPDATE")

    except Exception:
        db.rollback()
        return EmailErrorResponse(message="ERROR_PREPARE")
    

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
    
def get_all_customers(db: Session) :
    """
    Returns all users who are NOT vendors (alsoVendor = 0)
    Excludes sensitive fields like password
    """

    try : 
        users = db.query(User).filter(User.alsoVendor == 0).all()

        if not users :
            return NoUserResponse(message="NO CUSTOMERS FOUND")
        
        return [CustomerListItem(
            UID= user.UID,
            USERAPPID=user.userAppId,
            ALTERNATENUMBER=user.alternateNumber,
            FULLNAME=user.fullName,
            EMAILID=user.emailId,
            DOB=user.dob,
            CITY=user.city,
            GENDER=user.gender,
            PROFILEPICTURE=user.profilePicture,
            CUSTOMERRATING=user.customerRating,
            TOTALCUSTOMERREVIEWS=user.totalCustomerReviews,
            FCMTOKEN=user.fcmToken,
            JOININGDATE=user.joiningDate,
            CUSTSIGNUPDATE=user.custSignUpDate,
            CUSTNOOFTRIPSCOMPLETED=user.custNoOfTripsCompleted,
            BASELOCATION=user.baseLocation,
            USERLOGINSTATUS=user.user_login_status,
            LOCKAPP=user.lockApp,
            TABLETIMESTAMP=user.tableTimestamp
        ) for user in users]
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE_GET_ALL_CUSTOMERS")
    finally:
        db.close()




    #   try:
    #     users = db.query(User).filter(User.userAppId == userAppId).all()

    #     if not users:
    #         return NoUserResponse(message="NO REGISTERED")
        
    #     return [GetUserDetailsResponse(
    #         ALTERNATEMNUM=user.alternateNumber,
    #         FULLNAME=user.fullName,
    #         EMAILID=user.emailId,
    #         DOB=user.dob,
    #         CITY=user.city,
    #         GENDER=user.gender,
    #         PROFILEPIC=user.profilePicture,
    #         RATING=user.rating,
    #         TOTALREVIEWS=user.totalNoOfReviews,
    #         FCMTOKEN=user.fcmToken,
    #         USERLOGINSTATUS=user.user_login_status
    #     ) for user in users]
    # except SQLAlchemyError:
    #     return ErrorResponse(message="ERROR_PREPARE")
    # finally:
    #     db.close()


# def get_all_vendors_with_unapproved(db:Session):
#     try:
#         # --- Build lookup maps (exactly like your PHP) ---
#         region_map = {}
#         for r in db.query(Region).all():
#             region_map[str(r.RDID)] = r.regionName

#         city_map = {}
#         for loc in db.query(LocationDetail).all():
#             city_map[str(loc.LID)] = loc.location
        
#         request_type_map = {}
#         for rt in db.query(RequestType).all():
#             request_type_map[str(rt.RTDID)] = rt.requestType 

#         # Helper to convert "1,2,3" → "Name1, Name2, Name3"
#         def map_preference(pref_str : str, lookup_map : dict) -> str:
#             if not pref_str or str(pref_str).strip() == "":
#                 return ""
#             ids = [x.strip() for x in str(pref_str).split(",") if x.strip()]
#             names = [lookup_map.get(pid, pid) for pid in ids]
#             return ", ".join(names)
        
#         # --- Main query: ALL vendors (including unapproved) ---
#         vendors = db.query(User).filter(User.alsoVendor == 1).all()

#         if not vendors : 
#             return {"message":"NO VENDORS FOUND","data":[]}
        
#         result = []
#         for vendor in vendors :
#             region_names = map_preference(vendor.regionPreferences, region_map)
#             city_names = map_preference(vendor.cityPreferences, city_map)
#             request_type_names = map_preference(vendor.requestTypePreferences, request_type_map)    

#             result.append({
#                 'UID' : vendor.UID,
#                 'USERAPPID' : vendor.userAppId,
#                 'PASSWORD' : vendor.password,
#                 'ALTERNATENUMBER' : vendor.alternateNumber,
#                 'FULLNAME' : vendor.fullName,
#                 'EMAILID' : vendor.emailId,
#                 'DOB' : vendor.dob,
#                 'CITY' : vendor.city,
#                 'GENDER' : vendor.gender,
#                 'PROFILEPICTURE' : vendor.profilePicture,
#                 'CUSTOMERRATING' : vendor.customerRating,
#                 'RATING':vendor.rating,
#                 'TOTALNOOFREVIEWS' : vendor.totalNoOfReviews,
#                 'TOTALCUSTOMERREVIEWS' : vendor.totalCustomerReviews,
#                 'FCMTOKEN' : vendor.fcmToken,
#                 'JOININGDATE' : vendor.joiningDate,
#                 'CUSTSIGNDATE' : vendor.custSignUpDate,
#                 'CUSTNOOFTRIPSCOMPLETED' : vendor.custNoOfTripsCompleted,
#                 'BASELOCATION' : vendor.baseLocation,
#                 'USERLOGINSTATUS' : vendor.user_login_status,
#                 'ALSOVENDOR' : vendor.alsoVendor,
#                 'VENDORAPPROVED' : vendor.vendorApproved,
#                 'LOCKAPP' : vendor.lockApp,
#                 'TAGS' : vendor.tags,
#                 'NOOFTRIPSCOMPLETED' : vendor.noOfTripsCompleted,
#                 'DELETIONREASON' : vendor.deletionReason,
#                 'ADDDRESS' : vendor.address,
#                 'STATE' : vendor.state,
#                 'BANKACCOUNTHOLDERNAME': vendor.bankAccountHolderName,
#                 'BANKACCOUNTNO' : vendor.bankAccountNo,
#                 'BANKIFSC' : vendor.bankIFSC,
#                 'BANKNAME' : vendor.bankName,
#                 'IMAGEAADHAR' : vendor.imageAadhar,
#                 'IMAGEPAN' : vendor.imagePAN,
#                 'IMAGEBANKACCOUNT' : vendor.imageBankAccount,
#                 'REGIONPREFERENCES' : vendor.regionPreferences,
#                 'CITYPREFERENCES' : vendor.cityPreferences,
#                 'REQUESTTYPEPREFERENCES' : vendor.requestTypePreferences,
#                 # Human readable names (for frontend pills)
#                 'REGIONPREFERENCE_NAMES': region_names,
#                 'CITYPREFERENCE_NAMES': city_names,
#                 'REQUESTTYPEPREFERENCENAMES': request_type_names,
#                 'TABLETIMESTAMP': vendor.tableTimestamp                
#              })
            
#         return {"message":"SUCCESS","data":result, "total": len(result)}
#     except SQLAlchemyError:
#         return {"message":"ERROR_PREPARE"}
#     finally:
#         db.close()


def get_all_vendors_with_unapproved(db:Session):
    try:
        return get_all_vendors_enriched(db,approved_only=False)
    except SQLAlchemyError as e:
        print(str(e))
        return EmailErrorResponse(message="ERROR_PREPARE", error=str(e))


def get_admin_number(db:Session):
    try:
        record = db.query(AdminNumber).first()

        if not record:
            return NoUserResponse(message="NO_ADMIN_NUMBER_FOUND")
        
        return AdminNumberResponse(phonenumber=record.phonenumber)
    except SQLAlchemyError as e:
        print(str(e))
        return EmailErrorResponse(message="ERROR_PREPARE", error=str(e))
    finally:
        db.close()
        

def update_vendor_approved_status(db:Session, data : UpdateVendorApprovalRequest):
    try:
        user = db.query(User).filter(User.UID == data.UID).first()        
        if not user:
            return EmailErrorResponse(message="USER_NOT_FOUND")
        
        updated = False

        # Update vendorApproved only if needed
        if user.vendorApproved != data.vendorApproved:
            user.vendorApproved=data.vendorApproved
            updated = True

        # If approving, ensure tag "1" exists
        if data.vendorApproved:
            existing_tags = []

            if user.tags : 
                existing_tags = [tag.strip() for tag in user.tags.split(",") if tag.strip()]
            
            if "1" not in existing_tags:
                existing_tags.append("1")
                user.tags = ",".join(existing_tags)
                updated = True  
        
        if not updated:
            return EmailErrorResponse(message="NO_CHANGES")
        
        user.tableTimestamp = datetime.now()
        db.commit()

        return EmailErrorResponse(message="UPDATED")
    except SQLAlchemyError as e:
        db.rollback()
        print(str(e))
        return EmailErrorResponse(message="ERROR_UPDATE", error=str(e))
    finally:
        db.close()  


def update_vendor_lock_app_status(db:Session, data : UpdateVendorLockAppStatusRequest):
    try:
        user = db.query(User).filter(User.UID == data.UID).first()        
        if not user:
            return EmailErrorResponse(message="USER_NOT_FOUND")
        
        if user.lockApp == data.lockApp:
            return EmailErrorResponse(message="NO_CHANGES")
        
        user.lockApp = data.lockApp
        user.tableTimestamp = datetime.now()
        db.commit()

        return EmailErrorResponse(message="UPDATED")
    except SQLAlchemyError as e:
        db.rollback()
        print(str(e))
        return EmailErrorResponse(message="ERROR_UPDATE", error=str(e))
    finally:
        db.close()


def reject_user(db:Session, data : RejectUserRequest):
    try:
        user = (
            db.query(User)
            .filter(User.UID == data.userid)
            .first()
        )

        if not user:
            return EmailErrorResponse(message="USER_NOT_FOUND")
        
        #Snapshot before deletion for email content
        user_data = {
             "UID": user.UID,
            "userAppId": getattr(user, "userAppId", None),
            "fullName": getattr(user, "fullName", None),
            "emailId": getattr(user, "emailId", None),
            "alternateNumber": getattr(user, "alternateNumber", None),
            "city": getattr(user, "city", None),
            "state": getattr(user, "state", None),
            "baseLocation": getattr(user, "baseLocation", None),
            "CTD": getattr(user, "CTD", None),
            "joiningDate": getattr(user, "joiningDate", None),
            "bankAccountHolderName": getattr(user, "bankAccountHolderName", None),
            "bankAccountNo": getattr(user, "bankAccountNo", None),
            "bankIFSC": getattr(user, "bankIFSC", None),
            "bankName": getattr(user, "bankName", None),
            "rating": getattr(user, "rating", None),
            "noOfTripsCompleted": getattr(user, "noOfTripsCompleted", None),
            "vendorApproved": getattr(user, "vendorApproved", None),
            "lockApp": getattr(user, "lockApp", None),
            "imageAadhar": getattr(user, "imageAadhar", None),
            "imagePan": getattr(user, "imagePan", None),
            "imageBankAccount": getattr(user, "imageBankAccount", None),
        }

        db.delete(user)
        db.commit()

        # Send email notification about rejection
        try:
            ist_now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

            def fmt(value):
                return "" if value is None else str(value)
            
            def yes_no (value):
                return "Yes" if value else "No"
            
            def link_or_text(url):
                if url:
                    return f'<a href="{url}" target="_blank">View</a>'
                return '<span class="muted">Not Provided</span>'
            
            html = f"""
            <html>
            <head>
                <title>User Deleted - OpenBid</title>
                <style>
                    body {{ font-family: Arial, sans-serif; color: #333; background:#f5f7fb; }}
                    .wrap {{ max-width:760px; margin:24px auto; }}
                    .box {{ padding:18px 20px; background:#fff; border:1px solid #e6e9f0; border-radius:12px; }}
                    h2 {{ margin:0 0 12px; color:#1f2d3d; }}
                    .grid {{ display:grid; grid-template-columns:220px 1fr; gap:8px 14px; }}
                    .k {{ font-weight:600; color:#2c3e50; }}
                    .sep {{ height:1px; background:#eceff4; margin:14px 0; }}
                    a {{ color:#0b5ed7; text-decoration:none; }}
                    .muted {{ color:#6b7280; }}
                    .code {{ background:#f3f4f6; padding:8px 10px; border-radius:8px; }}
                </style>
            </head>
            <body>
                <div class="wrap">
                    <div class="box">
                        <h2>User Deleted from OpenBid</h2>

                        <div class="grid">
                            <div class="k">UID</div><div>{fmt(user_data["UID"])}</div>
                            <div class="k">User App ID</div><div>{fmt(user_data["userAppId"])}</div>
                            <div class="k">Full Name</div><div>{fmt(user_data["fullName"])}</div>
                            <div class="k">Email</div><div>{fmt(user_data["emailId"])}</div>
                            <div class="k">Alternate No.</div><div>{fmt(user_data["alternateNumber"])}</div>
                            <div class="k">City</div><div>{fmt(user_data["city"])}</div>
                            <div class="k">State</div><div>{fmt(user_data["state"])}</div>
                            <div class="k">Base Location</div><div>{fmt(user_data["baseLocation"])}</div>
                            <div class="k">Created Date</div><div>{fmt(user_data["CTD"])}</div>
                            <div class="k">Joining Date</div><div>{fmt(user_data["joiningDate"])}</div>
                        </div>

                        <div class="sep"></div>

                        <div class="grid">
                            <div class="k">Aadhar</div><div>{link_or_text(user_data["imageAadhar"])}</div>
                            <div class="k">PAN</div><div>{link_or_text(user_data["imagePan"])}</div>
                            <div class="k">Bank Passbook</div><div>{link_or_text(user_data["imageBankAccount"])}</div>
                        </div>

                        <div class="sep"></div>

                        <div class="grid">
                            <div class="k">Vendor Approved</div><div>{yes_no(user_data["vendorApproved"])}</div>
                            <div class="k">App Locked</div><div>{yes_no(user_data["lockApp"])}</div>
                            <div class="k">Trips Completed</div><div>{fmt(user_data["noOfTripsCompleted"])}</div>
                            <div class="k">Rating</div><div>{fmt(user_data["rating"])}</div>
                            <div class="k">Deleted At (IST)</div><div>{ist_now}</div>
                            <div class="k">Deleted By</div><div>{fmt(data.deletedBy) if data.deletedBy else '<span class="muted">Not provided</span>'}</div>
                            <div class="k">Reason</div><div>{f'<div class="code">{fmt(data.reason)}</div>' if data.reason else '<span class="muted">Not provided</span>'}</div>
                        </div>

                        <div class="sep"></div>
                        <div class="muted">This is an automated message. Please do not reply.</div>
                    </div>
                </div>
            </body>
            </html>
            """

            send_email(
                message=html,
                subject="OpenBid | User Rejected & Deleted",
                from_address="ticketdetails@wizzride.com",
                from_name="WizzRide Support",
                to_address="openbidresourceteam@wizzride.com",
                to_name="OpenBid Resource Team",                
            )

        except Exception:
            pass

        return EmailErrorResponse(message="DELETED")
    
    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="ERROR_DELETE", error=str(e))
    except Exception as e:
        db.rollback()
        return ErrorResponse(message="ERROR_DELETE", error=str(e))
    finally:
        db.close()
            

def upload_vendor_document_backend(
        db:Session,
        data : UploadVendorDocumentRequest
):
    try:
        vendor_id = data.vendorid
        doc_type = data.doctype
        upload_raw = data.uploadFile if isinstance(data.uploadFile, str) else ""

        if not vendor_id or not doc_type or not upload_raw:
            return EmailErrorResponse(message="MISSING_PARAMETERS", error="ERROR_INVALID_INPUT")
        
        user = db.query(User).filter(User.UID == vendor_id).first()

        if not user:
            return EmailErrorResponse(message="ERROR", error="VENDOR_NOT_FOUND")
        
        #PROFILEPICTURE
        if doc_type == "PROFILEPICTURE":
            result = upload_vendor_profile_picture_azure(vendor_id, upload_raw)

            if result["message"] != "UPLOADED":
                return ErrorResponse(
                    message="ERROR",
                    error=result.get("message", "ERROR_PROFILE_UPLOAD_FAILED")
                )
            
            new_url = result.get("url")
            old_url = getattr(user, "profilePicture", None)

            user.profilePicture = new_url
            user.tableTimestamp = datetime.now()
            db.commit()

            if old_url and old_url != new_url:
                try:
                    azure_blob_delete_by_url(old_url)
                except Exception:
                    pass
            
            return UploadVendorDocumentResponse(
                status="SUCCESS",
                doc_type=doc_type,
                column="profilePicture",
                vendor=vendor_id,
                url=new_url
            )
        
        # KYC docs
        doc_meta = {
            "IMAGEAADHAR": {"column": "imageAadhar", "slug": "Aadhaar"},
            "IMAGEPAN": {"column": "imagePAN", "slug": "PAN"},
            "IMAGEBANKACCOUNT": {"column": "imageBankAccount", "slug": "Bank"},        
        }

        if doc_type not in doc_meta:
            return EmailErrorResponse(message="ERROR", error="INVALID_DOCUMENT_TYPE")
        
        column_name = doc_meta[doc_type]["column"]
        slug = doc_meta[doc_type]["slug"]

        old_url = getattr(user, column_name, None)
        timestamp = datetime.now().strftime("%Y%M%D_%H%M%S")
        blob_name = f"{vendor_id}/{slug}_{timestamp}"

        ok, new_url = azure_blob_upload(blob_name, upload_raw,False)

        if not ok:
            return EmailErrorResponse(
                message="ERROR",
                error=new_url or "ERROR_UPLOAD_AZURE"
            )
        
        setattr(user, column_name, new_url)
        user.tableTimestamp = datetime.now()
        db.commit()

        if old_url and old_url != new_url:
            try:
                azure_blob_delete_by_url(old_url)
            except Exception:
                pass
        return UploadVendorDocumentResponse(
            status="SUCCESS",
            doc_type=doc_type,
            column=column_name,
            vendor=vendor_id,
            url=new_url
        )
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR", error=str(e))
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR", error=str(e))
    finally:
        db.close()
        
        



            
        
