import base64
import binascii
import io
import logging
import time
from zoneinfo import ZoneInfo

from PIL import Image
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
                                  UploadVendorDocumentResponse,VendorBankAccountSummaryResponse)
from ..utils.common import (
    ErrorResponse,
    ImageResponse,
    EmailErrorResponse,
    _ids_to_csv,
    _to_id_array,
    _csv_to_set,
    _parse_id_list_strict,
)
from ..utils.vendor_snapshot_refresh import request_vendor_snapshot_refresh
from ..utils.image import upload_image,upload_vendor_profile_picture_azure,azure_blob_upload,azure_blob_delete_by_url
from ..utils.email import send_email
from ..utils.fcm import subscribe_token_to_topics, TOPIC_ALL_USERS, TOPIC_ALL_VENDORS, unsubscribe_token_from_topics
from ..services.vendor_filtering import get_all_vendors_enriched
from datetime import date, datetime
from typing import Optional, Tuple
from fastapi import HTTPException, status
import re
import os
import html
from ..models.request_table import Request
from ..models.driver_details import DriverDetail
from ..utils.security import verify_and_update_password
from ..utils.rate_limit import enforce_rate_limit
import hashlib

_logger = logging.getLogger(__name__)

_MAX_PROFILE_IMAGE_BYTES = 2 * 1024 * 1024
_PROFILE_ALLOWED_FORMATS = {
    "jpeg": ("image/jpeg", "jpg"),
    "jpg": ("image/jpeg", "jpg"),
    "png": ("image/png", "png"),
}
_PROFILE_ALLOWED_MIMES = {"image/jpeg", "image/png"}
# Practical decompression-bomb guard (~25MP); Pillow default is higher.
_PROFILE_MAX_IMAGE_PIXELS = 25_000_000

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
    """PR6 session profile row(s). Does not close the request-scoped session."""
    try:
        users = db.query(User).filter(User.userAppId == str(userAppId).strip()).all()

        if not users:
            return NoUserResponse(message="NO REGISTERED")
        
        return [_to_get_user_details_response(user) for user in users]
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")


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


def _reject_user_app_id_mismatch(jwt_sub: str, user_app_id: Optional[str]) -> None:
    """Transitional compatibility: optional client userAppId must match JWT sub."""
    if user_app_id is None:
        return
    provided = str(user_app_id).strip()
    if not provided:
        return
    if provided != str(jwt_sub).strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )


def _enforce_bank_vendor_eligibility(user: User) -> None:
    """PR17 bank GET/PUT eligibility — ACCOUNT_LOCKED before VENDOR_NOT_ELIGIBLE."""
    _enforce_approved_vendor_eligibility(user)


def _enforce_approved_vendor_eligibility(user: User) -> None:
    """Approved unlocked vendor gate — ACCOUNT_LOCKED before VENDOR_NOT_ELIGIBLE."""
    if bool(getattr(user, "lockApp", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ACCOUNT_LOCKED",
        )
    if (
        not bool(getattr(user, "alsoVendor", False))
        or not bool(getattr(user, "vendorApproved", False))
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VENDOR_NOT_ELIGIBLE",
        )


def _mask_bank_account_mobile(account_no: Optional[str]) -> Optional[str]:
    """Masked mobile account display — never returns the full value.

    Uses the same last-four suffix rule as PR16 ``_mask_bank_account``,
    with a stable ``X`` mask character for the Flutter contract.
    """
    cleaned = _kyc_clean(account_no)
    if not cleaned:
        return None
    if len(cleaned) <= 4:
        return "XXXX"
    return f"{'X' * (len(cleaned) - 4)}{cleaned[-4:]}"


def _has_bank_account(account_no: Optional[str]) -> bool:
    return bool(_kyc_clean(account_no))


def _ist_now_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def get_user_bank_details(
    db: Session,
    user_id: str,
    user_app_id: Optional[str] = None,
) -> VendorBankAccountSummaryResponse:
    """PR17 GET /getregisteredbankaccount — JWT-owned masked bank summary."""
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, user_app_id)

    try:
        user = db.query(User).filter(User.userAppId == jwt_sub).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        _enforce_bank_vendor_eligibility(user)

        account_no = getattr(user, "bankAccountNo", None)
        has_account = _has_bank_account(account_no)
        if not has_account:
            return VendorBankAccountSummaryResponse(
                hasBankAccount=False,
                maskedAccountNumber=None,
                accountHolderName=None,
                bankIFSC=None,
                bankName=None,
            )

        return VendorBankAccountSummaryResponse(
            hasBankAccount=True,
            maskedAccountNumber=_mask_bank_account_mobile(account_no),
            accountHolderName=getattr(user, "bankAccountHolderName", None),
            bankIFSC=getattr(user, "bankIFSC", None),
            bankName=getattr(user, "bankName", None),
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to load bank details",
        ) from None

    
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

def fcm_token_update(
    db: Session,
    *,
    user_id: str,
    fcm_token: str,
    auth_subject: str | None = None,
):
    """PR36 — JWT-owned FCM token set. Does not bump tableTimestamp.

    PR38: rate-limit bucket uses hashed auth_subject when provided (never raw).
    """
    jwt_sub = str(user_id or "").strip()
    cleaned_token = str(fcm_token or "").strip()
    limit_identity = str(auth_subject or "").strip() or jwt_sub

    if not jwt_sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )

        if user is None or _is_tombstone_user(user):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        stored = (user.fcmToken or "").strip()
        if stored == cleaned_token:
            _logger.info(
                "fcm_token_update_same_value user_hash=%s",
                _safe_user_hash(limit_identity),
            )
            return ErrorResponse(message="UPDATED")

        limited = enforce_rate_limit(
            db,
            bucket_key=f"fcmtokenupdate:user:{_safe_user_hash(limit_identity)}",
            max_hits=int(os.getenv("RATE_LIMIT_FCM_TOKEN_UPDATE_PER_USER", "10")),
            window_seconds=int(
                os.getenv("RATE_LIMIT_FCM_TOKEN_UPDATE_WINDOW_SECONDS", "3600")
            ),
        )
        if limited is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="FCM_TOKEN_UPDATE_RATE_LIMITED",
            )

        user.fcmToken = cleaned_token
        # FCM registration is infrastructure state — do not bump tableTimestamp.
        db.commit()
        db.refresh(user)

        topics = [TOPIC_ALL_USERS]
        if bool(getattr(user, "alsoVendor", False)):
            topics.append(TOPIC_ALL_VENDORS)

        try:
            topic_result = subscribe_token_to_topics(cleaned_token, topics)
            if not topic_result.get("success"):
                _logger.info(
                    "fcm_token_topic_subscribe_partial user_hash=%s",
                    _safe_user_hash(limit_identity),
                )
        except Exception:
            _logger.info(
                "fcm_token_topic_subscribe_failed user_hash=%s",
                _safe_user_hash(limit_identity),
            )

        _logger.info(
            "fcm_token_update_changed user_hash=%s",
            _safe_user_hash(limit_identity),
        )
        return ErrorResponse(message="UPDATED")

    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        _logger.exception(
            "fcm_token_update_db_failed user_hash=%s",
            _safe_user_hash(limit_identity),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FCM_TOKEN_UPDATE_FAILED",
        ) from None
    except Exception:
        db.rollback()
        _logger.exception(
            "fcm_token_update_failed user_hash=%s",
            _safe_user_hash(limit_identity),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FCM_TOKEN_UPDATE_FAILED",
        ) from None


def _safe_user_hash(user_app_id: str) -> str:
    """Stable hashed user identifier for safe logs (never log raw phone)."""
    digest = hashlib.sha256(str(user_app_id).encode("utf-8")).hexdigest()
    return digest[:12]


def logout_user(db: Session, user_app_id: str, fcm_token: Optional[str] = None):
    """Clear login status and stored FCM token for the requested userAppId.

    ``fcm_token`` is accepted for endpoint signature compatibility only.
    Unsubscribe always uses the previously stored DB token.
    """
    try:
        user = db.query(User).filter(User.userAppId == user_app_id).first()
        if not user:
            return ErrorResponse(message="LOGOUT_FAILED")

        old_token = (user.fcmToken or "").strip()
        topics = [TOPIC_ALL_USERS]

        if bool(getattr(user, "alsoVendor", False)):
            topics.append(TOPIC_ALL_VENDORS)

        status_value = "LOGGEDOUT"
        user.user_login_status = status_value
        user.fcmToken = None

        db.commit()

        if old_token and old_token.lower() not in {"null", "none", "na"}:
            try:
                unsubscribe_token_from_topics(old_token, topics)
            except Exception:
                pass

        return LogoutResponse(
            messsage="LOGOUT_SUCCESS",
            status=status_value,
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

_DELETION_ACTIVE_BID_STATUSES = frozenset(
    {"BID - OPEN", "BID - CONFIRMED", "REQUEST - CONFIRMED"}
)
_DELETION_CANCELLED_REQUEST_STATUSES = frozenset(
    {
        "REQUEST - CANCELLED BY USER",
        "BOOKING - CANCELLED BY USER",
    }
)
_USER_APP_ID_MAX_LEN = 64


def _request_pickup_datetime(request_row: Request) -> datetime:
    return datetime.combine(request_row.pickUpDate, request_row.pickUpTime)


def _evaluate_deletion_lifecycle_gates(db: Session, jwt_sub: str) -> None:
    """Return the first blocking lifecycle gate as HTTP 409, else None."""
    now_ist = _ist_now_naive()

    open_req = (
        db.query(Request.RID)
        .filter(
            Request.customerAppId == jwt_sub,
            Request.requestStatus == "BID - OPEN",
        )
        .first()
    )
    if open_req is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="DELETION_BLOCKED_OPEN_REQUEST",
        )

    handshake = (
        db.query(Request.RID)
        .filter(
            Request.customerAppId == jwt_sub,
            Request.requestStatus == "BID - CONFIRMED",
        )
        .first()
    )
    if handshake is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="DELETION_BLOCKED_HANDSHAKE",
        )

    future_customer = (
        db.query(Request)
        .filter(
            Request.customerAppId == jwt_sub,
            Request.requestStatus == "REQUEST - CONFIRMED",
        )
        .all()
    )
    for row in future_customer:
        if _request_pickup_datetime(row) >= now_ist:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="DELETION_BLOCKED_FUTURE_BOOKING",
            )

    future_vendor = (
        db.query(Request)
        .filter(
            Request.requestWonBy == jwt_sub,
            Request.requestStatus == "REQUEST - CONFIRMED",
        )
        .all()
    )
    for row in future_vendor:
        if _request_pickup_datetime(row) >= now_ist:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="DELETION_BLOCKED_FUTURE_VENDOR_TRIP",
            )

    bidder_key = int(jwt_sub) if jwt_sub.isdigit() else None
    if bidder_key is not None:
        active_bids = (
            db.query(BidDetail, Request)
            .join(Request, Request.RID == BidDetail.rID)
            .filter(
                BidDetail.bidderID == bidder_key,
                BidDetail.bidStatus.in_(tuple(_DELETION_ACTIVE_BID_STATUSES)),
            )
            .all()
        )
        for _bid, req in active_bids:
            status_text = str(getattr(req, "requestStatus", "") or "")
            if status_text not in _DELETION_CANCELLED_REQUEST_STATUSES:
                if status_text in _DELETION_ACTIVE_BID_STATUSES:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="DELETION_BLOCKED_ACTIVE_BID",
                    )

    drivers = (
        db.query(DriverDetail)
        .filter(DriverDetail.userAppId == jwt_sub)
        .all()
    )
    driver_ids = [
        int(d.DDID)
        for d in drivers
        if getattr(d, "DDID", None) is not None
    ]
    if driver_ids:
        assigned = (
            db.query(Request)
            .filter(
                Request.driverAssignedID.in_(driver_ids),
                Request.requestStatus == "REQUEST - CONFIRMED",
            )
            .all()
        )
        for row in assigned:
            if _request_pickup_datetime(row) >= now_ist:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="DELETION_BLOCKED_ASSIGNED_DRIVER",
                )


def _generate_unique_tombstone_id(db: Session, original_user_app_id: str) -> str:
    """Canonical FastAPI tombstone: {id}.DELETED, {id}.DELETED1, ..."""
    base = f"{original_user_app_id}.DELETED"
    if len(base) > _USER_APP_ID_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ACCOUNT_DELETION_FAILED",
        )
    candidate = base
    counter = 1
    while db.query(User).filter(User.userAppId == candidate).first() is not None:
        candidate = f"{base}{counter}"
        if len(candidate) > _USER_APP_ID_MAX_LEN:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ACCOUNT_DELETION_FAILED",
            )
        counter += 1
        if counter > 1000:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ACCOUNT_DELETION_FAILED",
            )
    return candidate


def delete_user(db: Session, user_data: UserDelete, user_id: str):
    """PR24 JWT-owned soft tombstone deletion.

    Does not close the request-scoped DB session.
    Does not hard-delete related rows, blobs, or chat.
    """
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, getattr(user_data, "userAppId", None))

    old_token: Optional[str] = None
    topics = [TOPIC_ALL_USERS]
    also_vendor = False

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None or _is_tombstone_user(user):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        ok, new_hash = verify_and_update_password(
            user_data.password,
            user.password,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="WRONG_PASSWORD",
            )
        # Password upgrade participates in the same deletion transaction only.
        if new_hash:
            user.password = new_hash

        _evaluate_deletion_lifecycle_gates(db, jwt_sub)

        tombstone_id = _generate_unique_tombstone_id(db, jwt_sub)

        old_token = (user.fcmToken or "").strip()
        also_vendor = bool(getattr(user, "alsoVendor", False))
        if also_vendor:
            topics.append(TOPIC_ALL_VENDORS)

        user.userAppId = tombstone_id
        user.lockApp = True
        user.user_login_status = "LOGGEDOUT"
        user.deletionReason = user_data.deletionReason
        user.fcmToken = None
        # PR37: revoke all outstanding tokens for this account row
        user.sessionVersion = int(user.sessionVersion or 1) + 1
        # accountSessionId retained on tombstone (phone-reuse protection)
        user.tableTimestamp = _ist_now_naive()

        db.commit()
        _logger.info("account_deletion_committed")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        _logger.exception("account_deletion_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ACCOUNT_DELETION_FAILED",
        ) from None
    except Exception:
        db.rollback()
        _logger.exception("account_deletion_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ACCOUNT_DELETION_FAILED",
        ) from None

    if old_token and old_token.lower() not in {"null", "none", "na"}:
        try:
            unsubscribe_token_from_topics(old_token, topics)
        except Exception:
            _logger.info("account_deletion_fcm_unsubscribe_failed")

    return ErrorResponse(message="DELETED")
 
def update_vendor_bank_details(
    db: Session,
    user_data: UserBankDetailsUpdate,
    user_id: str,
) -> ErrorResponse:
    """PR17 PUT /updatevendorbankdetails — JWT-owned four-field bank text update."""
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, getattr(user_data, "userAppId", None))

    holder = str(user_data.bankAccountHolderName).strip()
    account_no = str(user_data.bankAccountNo).strip()
    ifsc = str(user_data.bankIFSC).strip().upper()
    bank_name = str(user_data.bankName).strip()

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        _enforce_bank_vendor_eligibility(user)

        unchanged = (
            _kyc_clean(getattr(user, "bankAccountHolderName", None)) == holder
            and _kyc_clean(getattr(user, "bankAccountNo", None)) == account_no
            and _kyc_clean(getattr(user, "bankIFSC", None)).upper() == ifsc
            and _kyc_clean(getattr(user, "bankName", None)) == bank_name
        )
        if unchanged:
            db.commit()
            return ErrorResponse(message="UPDATED")

        user.bankAccountHolderName = holder
        user.bankAccountNo = account_no
        user.bankIFSC = ifsc
        user.bankName = bank_name
        user.tableTimestamp = _ist_now_naive()
        db.commit()
        return ErrorResponse(message="UPDATED")
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to update bank details",
        ) from None

def _profile_blob_path_key(url: Optional[str]) -> str:
    """URL without query string for same-path overwrite comparison."""
    if not url:
        return ""
    return str(url).split("?", 1)[0].strip()


def _is_tombstone_user(user: User) -> bool:
    """Deleted / renamed tombstone rows (``*.DELETED*``) may not update profile images."""
    app_id = str(getattr(user, "userAppId", "") or "")
    return ".DELETED" in app_id.upper()


def _decode_profile_image_payload(raw: str) -> Tuple[bytes, Optional[str]]:
    """Decode raw or data-URI base64. Returns (bytes, claimed_mime_or_None)."""
    image_str = str(raw or "").strip()
    if not image_str:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_PROFILE_IMAGE",
        )

    claimed_mime: Optional[str] = None
    header_match = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,",
        image_str,
        flags=re.IGNORECASE,
    )
    if header_match:
        claimed_mime = header_match.group(1).lower()
        if claimed_mime == "image/jpg":
            claimed_mime = "image/jpeg"
        image_str = image_str[len(header_match.group(0)) :]
    elif image_str.lower().startswith("data:"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_PROFILE_IMAGE",
        )

    clean = re.sub(r"\s+", "", image_str)
    if not clean:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_PROFILE_IMAGE",
        )

    try:
        binary = base64.b64decode(clean, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_PROFILE_IMAGE",
        ) from None

    if not binary:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_PROFILE_IMAGE",
        )

    return binary, claimed_mime


def _validate_profile_image_bytes(
    binary: bytes,
    claimed_mime: Optional[str],
) -> Tuple[str, str]:
    """Validate decoded bytes; return (mime, ext) for JPEG/PNG only."""
    if len(binary) > _MAX_PROFILE_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PROFILE_IMAGE_TOO_LARGE",
        )

    if claimed_mime is not None and claimed_mime not in _PROFILE_ALLOWED_MIMES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="UNSUPPORTED_PROFILE_IMAGE_TYPE",
        )

    previous_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _PROFILE_MAX_IMAGE_PIXELS
    try:
        try:
            with Image.open(io.BytesIO(binary)) as img:
                img.verify()
                fmt = (img.format or "").lower()
        except Image.DecompressionBombError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="INVALID_PROFILE_IMAGE",
            ) from None
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="INVALID_PROFILE_IMAGE",
            ) from None

        try:
            with Image.open(io.BytesIO(binary)) as img:
                img.load()
                fmt = (img.format or fmt or "").lower()
        except Image.DecompressionBombError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="INVALID_PROFILE_IMAGE",
            ) from None
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="INVALID_PROFILE_IMAGE",
            ) from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max

    if fmt not in _PROFILE_ALLOWED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="UNSUPPORTED_PROFILE_IMAGE_TYPE",
        )

    mime, ext = _PROFILE_ALLOWED_FORMATS[fmt]
    if claimed_mime is not None and claimed_mime != mime:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_PROFILE_IMAGE",
        )

    return mime, ext


def _append_cache_buster(file_url: str) -> str:
    """Always append a fresh ``v=`` so successive uploads differ."""
    url = str(file_url)
    if "?" in url:
        base, query = url.split("?", 1)
        parts = [p for p in query.split("&") if p and not p.startswith("v=")]
        stamp = str(int(time.time() * 1000))
        if parts:
            return f"{base}?{'&'.join(parts)}&v={stamp}"
        return f"{base}?v={stamp}"
    return f"{url}?v={int(time.time() * 1000)}"


def _safe_delete_blob(url: Optional[str]) -> None:
    if not url:
        return
    try:
        azure_blob_delete_by_url(url)
    except Exception:
        _logger.warning("profile_image_blob_cleanup_failed")


def profile_image_upload(db: Session, user_data: UserImageUpload, user_id: str):
    """PR23 JWT-owned profile-image upload.

    Updates only ``profilePicture``. Does not close the request-scoped session.
    """
    jwt_sub = str(user_id or "").strip()
    if not jwt_sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    _reject_user_app_id_mismatch(jwt_sub, getattr(user_data, "userAppId", None))

    binary, claimed_mime = _decode_profile_image_payload(getattr(user_data, "image", ""))
    mime, _ext = _validate_profile_image_bytes(binary, claimed_mime)

    normalized_base64 = (
        f"data:{mime};base64," + base64.b64encode(binary).decode("ascii")
    )
    blob_name = f"{jwt_sub}_profile"

    new_blob_url: Optional[str] = None
    old_url: Optional[str] = None
    committed = False

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )
        if _is_tombstone_user(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="PROFILE_UPDATE_NOT_ALLOWED",
            )

        old_url = getattr(user, "profilePicture", None)

        ok, file_url = azure_blob_upload(
            blob_name=blob_name,
            base64_data=normalized_base64,
            make_public=True,
            max_upload_bytes=_MAX_PROFILE_IMAGE_BYTES,
        )
        if not ok:
            err = str(file_url or "").upper()
            if "FILE_TOO_LARGE" in err:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="PROFILE_IMAGE_TOO_LARGE",
                )
            if "UNSUPPORTED_IMAGE" in err:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="UNSUPPORTED_PROFILE_IMAGE_TYPE",
                )
            if "INVALID" in err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="INVALID_PROFILE_IMAGE",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="PROFILE_UPLOAD_FAILED",
            )

        new_blob_url = str(file_url)
        versioned_url = _append_cache_buster(new_blob_url)

        user.profilePicture = versioned_url
        user.tableTimestamp = _ist_now_naive()
        db.commit()
        committed = True

        old_key = _profile_blob_path_key(old_url)
        new_key = _profile_blob_path_key(new_blob_url)
        if old_key and new_key and old_key != new_key:
            _safe_delete_blob(old_url)

        return ImageResponse(message="UPLOADED", url=versioned_url)

    except HTTPException:
        if not committed:
            db.rollback()
            if new_blob_url is not None:
                _safe_delete_blob(new_blob_url)
        raise
    except SQLAlchemyError:
        db.rollback()
        if new_blob_url is not None:
            _safe_delete_blob(new_blob_url)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PROFILE_UPLOAD_FAILED",
        ) from None
    except Exception:
        db.rollback()
        if new_blob_url is not None:
            _safe_delete_blob(new_blob_url)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PROFILE_UPLOAD_FAILED",
        ) from None



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


_MAX_KYC_MEDIA_BYTES = 2 * 1024 * 1024
_DEFAULT_KYC_EMAIL_FROM = "customersupport@wizzride.com"


def _kyc_clean(v) -> str:
    return str(v or "").strip()


def _mask_bank_account(account_no: str) -> str:
    digits = _kyc_clean(account_no)
    if len(digits) <= 4:
        return "****"
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def _map_kyc_media_error(upload_message: Optional[str], label: str) -> str:
    msg = str(upload_message or "").upper()
    if "FILE_TOO_LARGE" in msg:
        return "ERROR_MEDIA_TOO_LARGE"
    if "INVALID_BASE64" in msg:
        return "ERROR_INVALID_MEDIA"
    if "INVALID_IMAGE" in msg or "UNSUPPORTED_IMAGE" in msg:
        return "ERROR_INVALID_MEDIA"
    return f"ERROR_SAVE_{label}"


def _cleanup_kyc_blobs(urls: list[str]) -> None:
    for url in urls:
        if not url:
            continue
        try:
            azure_blob_delete_by_url(url)
        except Exception:
            pass


def vendor_update_with_kyc(
    db: Session,
    vendor_update_data: VendorKycCreate,
    user_id: str,
):
    """PUT /registernewvendor — JWT-owned KYC upsert (PR16).

    Forces alsoVendor=true. Never sets vendorApproved. Never changes lockApp.
    Approved vendors are blocked with ALREADY_VENDOR. Pending vendors may resubmit.
    """
    EMAIL_TO = "openbidresourceteam@wizzride.com"
    EMAIL_TO_NAME = "Wizzride"
    EMAIL_FROM = os.getenv("KYC_EMAIL_FROM", _DEFAULT_KYC_EMAIL_FROM).strip() or _DEFAULT_KYC_EMAIL_FROM
    EMAIL_FROM_NAME = os.getenv("KYC_EMAIL_FROM_NAME", "WizzRide").strip() or "WizzRide"
    EMAIL_SUBJECT = "New/Updated Vendor Registration - Approval Pending"

    tz = ZoneInfo("Asia/Kolkata")
    jwt_sub = _kyc_clean(user_id).replace(" ", "")
    if not jwt_sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    # Legacy body identity must match JWT when supplied.
    legacy_id = getattr(vendor_update_data, "userAppId", None)
    if legacy_id is not None and _kyc_clean(legacy_id).replace(" ", ""):
        if _kyc_clean(legacy_id).replace(" ", "") != jwt_sub:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized",
            )

    required_fields = {
        "dob": getattr(vendor_update_data, "dob", None),
        "gender": getattr(vendor_update_data, "gender", None),
        "addressLine1": getattr(vendor_update_data, "addressLine1", None),
        "addressLine2": getattr(vendor_update_data, "addressLine2", None),
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
        if field_value is None or _kyc_clean(field_value) == "":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"ERROR_MISSING_{field_name.upper()}",
            )

    dob_raw = _kyc_clean(vendor_update_data.dob)
    try:
        dob_sql = datetime.strptime(dob_raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_DOB",
        )

    gender = _kyc_clean(vendor_update_data.gender)
    if gender not in {"Male", "Female", "Other"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_GENDER",
        )

    address1 = _kyc_clean(vendor_update_data.addressLine1)
    address2 = _kyc_clean(vendor_update_data.addressLine2)
    address = f"{address1} {address2}".strip()
    city = _kyc_clean(vendor_update_data.city)
    state = _kyc_clean(vendor_update_data.state)
    acc_holder = _kyc_clean(vendor_update_data.bankAccountHolderName)
    acc_no = _kyc_clean(vendor_update_data.bankAccountNo)
    ifsc = _kyc_clean(vendor_update_data.bankIFSC).upper()
    bank_name = _kyc_clean(vendor_update_data.bankName)

    if not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", ifsc):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_IFSC",
        )
    if not re.match(r"^[0-9A-Za-z\-]{6,22}$", acc_no):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_ACCOUNTNO",
        )

    joining_date_today = datetime.now(tz).date()
    new_blob_urls: list[str] = []
    old_aadhaar_url = None
    old_pan_url = None
    old_bank_url = None
    aadhaar_url = None
    pan_url = None
    bank_url = None

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        if bool(getattr(user, "lockApp", False)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ACCOUNT_LOCKED",
            )

        if bool(getattr(user, "vendorApproved", False)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ALREADY_VENDOR",
            )

        old_aadhaar_url = user.imageAadhar
        old_pan_url = user.imagePAN
        old_bank_url = user.imageBankAccount

        ts = datetime.now(tz).strftime("%Y%m%d_%H%M%S")
        blob_base = f"{jwt_sub}/"

        ok_a, aadhaar_url = azure_blob_upload(
            blob_name=f"{blob_base}Aadhaar_{ts}",
            base64_data=vendor_update_data.imageAadhar,
            make_public=False,
            max_upload_bytes=_MAX_KYC_MEDIA_BYTES,
        )
        if not ok_a:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=_map_kyc_media_error(aadhaar_url, "AADHAAR"),
            )
        new_blob_urls.append(aadhaar_url)

        ok_p, pan_url = azure_blob_upload(
            blob_name=f"{blob_base}PAN_{ts}",
            base64_data=vendor_update_data.imagePAN,
            make_public=False,
            max_upload_bytes=_MAX_KYC_MEDIA_BYTES,
        )
        if not ok_p:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=_map_kyc_media_error(pan_url, "PAN"),
            )
        new_blob_urls.append(pan_url)

        ok_b, bank_url = azure_blob_upload(
            blob_name=f"{blob_base}Bank_{ts}",
            base64_data=vendor_update_data.imageBankAccount,
            make_public=False,
            max_upload_bytes=_MAX_KYC_MEDIA_BYTES,
        )
        if not ok_b:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=_map_kyc_media_error(bank_url, "BANK"),
            )
        new_blob_urls.append(bank_url)

        # Re-check approval / lock under a fresh locked read before mutation apply
        # so an admin approval racing with upload is observed.
        db.refresh(user)
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )
        if bool(getattr(user, "vendorApproved", False)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ALREADY_VENDOR",
            )
        if bool(getattr(user, "lockApp", False)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ACCOUNT_LOCKED",
            )
        preserved_vendor_approved = bool(getattr(user, "vendorApproved", False))
        preserved_lock_app = bool(getattr(user, "lockApp", False))

        if not getattr(user, "joiningDate", None):
            user.joiningDate = joining_date_today

        existing_prefs = _kyc_clean(getattr(user, "requestTypePreferences", None))
        if not existing_prefs:
            user.requestTypePreferences = "1,2,3,4"

        user.alsoVendor = True
        user.vendorApproved = preserved_vendor_approved
        user.lockApp = preserved_lock_app
        user.dob = dob_sql
        user.address = address
        user.city = city
        user.gender = gender
        user.state = state
        user.bankAccountHolderName = acc_holder
        user.bankAccountNo = acc_no
        user.bankIFSC = ifsc
        user.bankName = bank_name
        user.imageAadhar = aadhaar_url
        user.imagePAN = pan_url
        user.imageBankAccount = bank_url

        db.commit()

        for old_url in [old_aadhaar_url, old_pan_url, old_bank_url]:
            if old_url and old_url not in new_blob_urls:
                try:
                    azure_blob_delete_by_url(old_url)
                except Exception:
                    # Cleanup failure must not undo committed registration.
                    pass

        masked_account = _mask_bank_account(acc_no)
        esc = html.escape
        email_html = f'''
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

                <div class="row"><span class="k">User App ID:</span> {esc(jwt_sub)}</div>
                <div class="row"><span class="k">Also Vendor:</span> Yes</div>
                <div class="row"><span class="k">Vendor Approved:</span> No (pending review)</div>

                <hr>
                <div class="row"><span class="k">DOB:</span> {esc(dob_sql)}</div>
                <div class="row"><span class="k">Gender:</span> {esc(gender)}</div>
                <div class="row"><span class="k">Address:</span> {esc(address)}</div>
                <div class="row"><span class="k">City / State:</span> {esc(city)} / {esc(state)}</div>

                <hr>
                <div class="row"><span class="k">Bank A/C Holder:</span> {esc(acc_holder)}</div>
                <div class="row"><span class="k">Account No:</span> {esc(masked_account)}</div>
                <div class="row"><span class="k">IFSC:</span> {esc(ifsc)}</div>
                <div class="row"><span class="k">Bank Name:</span> {esc(bank_name)}</div>

                <hr>
                <div class="row"><span class="k">Aadhaar:</span> <a href="{esc(aadhaar_url or '', quote=True)}" target="_blank">Private document</a></div>
                <div class="row"><span class="k">PAN:</span> <a href="{esc(pan_url or '', quote=True)}" target="_blank">Private document</a></div>
                <div class="row"><span class="k">Bank Doc:</span> <a href="{esc(bank_url or '', quote=True)}" target="_blank">Private document</a></div>

                <hr>
                <div class="row">Submitted at: {datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")}</div>
            </div>
        </body>
        </html>
        '''

        try:
            send_email(
                message=email_html,
                subject=EMAIL_SUBJECT,
                from_address=EMAIL_FROM,
                from_name=EMAIL_FROM_NAME,
                to_address=EMAIL_TO,
                to_name=EMAIL_TO_NAME,
            )
        except Exception:
            # Email failure must not undo successful registration.
            pass

        return EmailErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        _cleanup_kyc_blobs(new_blob_urls)
        raise
    except SQLAlchemyError:
        db.rollback()
        _cleanup_kyc_blobs(new_blob_urls)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ERROR",
        )
    except Exception:
        db.rollback()
        _cleanup_kyc_blobs(new_blob_urls)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ERROR",
        )

    

def update_request_type_selections(
    db: Session,
    data: UpdateRequestTypeSelectionsRequest,
    user_id: str,
):
    """PR18 PUT /updaterequesttypeselections — JWT-owned request-type prefs."""
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, getattr(data, "userAppId", None))

    try:
        new_ids = _parse_id_list_strict(
            data.requestTypeIds, field_name="REQUESTTYPEIDS"
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_REQUESTTYPEIDS",
        ) from None

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        _enforce_approved_vendor_eligibility(user)

        if new_ids:
            valid_ids = (
                db.query(RequestType.RTDID)
                .filter(RequestType.RTDID.in_(new_ids))
                .all()
            )
            valid_set = {row[0] for row in valid_ids}
            if len(valid_set) != len(new_ids):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="ERROR_INVALID_REQUESTTYPEIDS",
                )

        curr_csv = user.requestTypePreferences or ""
        next_csv = _ids_to_csv(new_ids)

        if next_csv == curr_csv:
            db.commit()
            return ErrorResponse(message="UPDATED")

        user.requestTypePreferences = next_csv
        # tableTimestamp: preference CSVs are feed filters, not profile text.
        # Worker/WSS propagation does not use tableTimestamp — skip churn.
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to update request type preferences",
        ) from None

    # After commit only — failure must not roll back business mutation.
    request_vendor_snapshot_refresh(jwt_sub)
    return ErrorResponse(message="UPDATED")


def update_region_city_selections(
    db: Session,
    data: UpdateRegionCitySelectionsRequest,
    user_id: str,
):
    """PR18 PUT /updateregioncityselections — JWT-owned atomic region/city prefs."""
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, getattr(data, "userAppId", None))

    try:
        new_region_ids = _parse_id_list_strict(
            data.regionIds, field_name="REGIONIDS"
        )
        new_city_ids = _parse_id_list_strict(
            data.cityIds, field_name="CITYIDS"
        )
    except ValueError as exc:
        detail = str(exc) if str(exc).startswith("ERROR_INVALID_") else "ERROR_INVALID_REGIONIDS"
        if "CITYIDS" in str(exc):
            detail = "ERROR_INVALID_CITYIDS"
        elif "REGIONIDS" in str(exc):
            detail = "ERROR_INVALID_REGIONIDS"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        ) from None

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .with_for_update()
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        _enforce_approved_vendor_eligibility(user)

        if new_region_ids:
            valid_regions = (
                db.query(Region.RDID)
                .filter(Region.RDID.in_(new_region_ids))
                .all()
            )
            found_region_ids = {row[0] for row in valid_regions}
            if len(found_region_ids) != len(new_region_ids):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="ERROR_INVALID_REGIONIDS",
                )

        if new_city_ids:
            valid_cities = (
                db.query(LocationDetail.LID)
                .filter(LocationDetail.LID.in_(new_city_ids))
                .all()
            )
            found_city_ids = {row[0] for row in valid_cities}
            if len(found_city_ids) != len(new_city_ids):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="ERROR_INVALID_CITYIDS",
                )

        cur_region_csv = user.regionPreferences or ""
        cur_city_csv = user.cityPreferences or ""
        next_region_csv = _ids_to_csv(new_region_ids)
        next_city_csv = _ids_to_csv(new_city_ids)

        if next_region_csv == cur_region_csv and next_city_csv == cur_city_csv:
            db.commit()
            return ErrorResponse(message="UPDATED")

        user.regionPreferences = next_region_csv
        user.cityPreferences = next_city_csv
        # tableTimestamp unused for WSS preference propagation — skip churn.
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to update region/city preferences",
        ) from None

    request_vendor_snapshot_refresh(jwt_sub)
    return ErrorResponse(message="UPDATED")


def get_request_type_selections(
    db: Session,
    user_id: str,
    user_app_id: Optional[str] = None,
):
    """PR18 GET /getuserrequesttypepreferences — JWT-owned catalog + SELECTED."""
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, user_app_id)

    try:
        user = db.query(User).filter(User.userAppId == jwt_sub).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        _enforce_approved_vendor_eligibility(user)

        req_type_csv = user.requestTypePreferences or ""
        selected_ids = _csv_to_set(req_type_csv)

        types_raw = (
            db.query(RequestType.RTDID, RequestType.requestType)
            .order_by(RequestType.requestType.asc())
            .all()
        )

        types = []
        for rtdid, rtype in types_raw:
            types.append(
                {
                    "REQUEST_TYPE_ID": rtdid,
                    "REQUEST_TYPE_NAME": rtype or "",
                    "SELECTED": rtdid in selected_ids,
                }
            )

        types.sort(key=lambda x: x["REQUEST_TYPE_NAME"].lower())
        return [RequestTypeResponse(**t) for t in types]
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to load request type preferences",
        ) from None

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
        doc_type = data.docType
        upload_raw = data.uploadFile if isinstance(data.uploadFile, str) else ""

        if not vendor_id or not doc_type or not upload_raw:
            return EmailErrorResponse(message="MISSING_PARAMETERS", error="ERROR_INVALID_INPUT")
        
        user = db.query(User).filter(User.userAppId == vendor_id).first()

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
                docType=doc_type,
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
            docType=doc_type,
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
        
        



            
        
