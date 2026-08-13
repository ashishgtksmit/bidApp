import logging
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Union
from zoneinfo import ZoneInfo
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models.request_table import Request
from ..models.bid_details import BidDetail
from ..models.user_table import User
from ..models.driver_details import DriverDetail
from ..models.request_type_details import RequestType
from ..models.customer_reviews import CustomerReview
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..models.bid_details import BidDetail
from ..schemas.request_table import (RequestResponse,NoBidsResponse,RequestByRidResponse,RequestUpdate,
                                     RequestConfirmedForUserResponse,RequestConfirmedForVendorResponse,
                                     RequestCreate,AssignDriverRequest,RequestForUserResponse,
                                     RequestConfirmedCommonResponse,GetBookingReportResponse,
                                     ReopenBookingResponse)
from ..schemas.booking_history import (
    CustomerBookingHistoryItem,
    VendorBookingHistoryItem,
    VendorCancelledHistoryItem,
)
from ..schemas.request_type_details import RequestTypeBase
from ..utils.common import ErrorResponse,EmailErrorResponse, FCMSend

logger = logging.getLogger(__name__)

_CONFIRMED_STATUS = "REQUEST - CONFIRMED"
from ..services.notifications import (
    FCMSendDrivers,
    notify_driver_assigned_to_customer_background,
    notify_vendors_for_request,
    notify_vendors_request_cancelled,
    notify_vendor_booking_cancelled_by_customer,
    send_notification_to_selected_users,
    send_notification_to_user,
)
from ..services.vendor_filtering import get_other_vendors_who_bid_on_request, get_vendors_for_request,get_vendors_who_bid_on_request
from ..events.outbox import (
    log_handshake_cancelled_emission_decision,
    maybe_append_domain_event,
)
from ..events.registry import (
    EVENT_BOOKING_CANCELLED_BY_CUSTOMER,
    EVENT_DRIVER_ASSIGNMENT_CHANGED,
    EVENT_HANDSHAKE_CANCELLED,
    EVENT_REQUEST_CREATED,
)

# MySQL TEXT max for rejectionReason — do not expose column name in errors.
_REJECTION_REASON_MAX_LEN = 65535

STATUS_REQUEST_CONFIRMED = "REQUEST - CONFIRMED"
STATUS_BOOKING_CANCELLED_BY_USER = "BOOKING - CANCELLED BY USER"
STATUS_BID_OPEN = "BID - OPEN"

def get_all_open_requests(db : Session):
    try:
        # with db.begin():
            request_status = 'BID - OPEN'
            requests = db.query(Request).filter(Request.requestStatus == request_status).all()
            if not requests : 
                return NoBidsResponse(message="NO BIDS FOUND")
            return [RequestConfirmedCommonResponse(
                REQUESTID=req.RID,
                FROMLOCATION=req.fromLocation,
                FROMLANDMARK=req.fromLandmark,
                TOLOCATION=req.toLocation,
                TOLANDMARK=req.toLandmark,
                PICKUPDATE=req.pickUpDate,
                PICKUPTIME=req.pickUpTime,
                NOOFADULTS=req.noOfAdults,
                NOOFKIDS=req.noOfKids,
                CARTYPE=req.carType,
                ACREQUEST=req.acRequest,
                CARRIERREQUES=req.carrierRequest,
                BIDENDTIME=req.bidEndTime,
                REQUESTSTATUS=req.requestStatus,
                PAYMENTSTATUS=req.paymentStatus,
                CUSTOMERAPPID=req.customerAppId,
                REQUESTWONBY=req.requestWonBy,
                NOOFBIDS=req.noOfBids,
                TABLETIMESTAMP=req.tableTimestamp
            ) for req in requests]
    except SQLAlchemyError as e:
        db.rollback()
        print(str(e))
        return NoBidsResponse(message="ERROR_PREPARE")
    finally:
        db.close()


# def get_all_requests_for_user(db : Session, customer_app_id : str):    
#     try:
#         requests = db.query(
#                 Request,
#                 DriverDetail.driverName,
#                 DriverDetail.driverNumber,
#                 DriverDetail.driverPhoto,
#                 DriverDetail.driverDOB,
#                 DriverDetail.driverGender,
#                 DriverDetail.driverCity,
#                 DriverDetail.driverLicense,
#                 BidDetail.bidAmount,
#                 BidDetail.CARID,
#                 CarDetail.carRegNo,
#                 CarDetail.carModel,
#                 CarDetail.modelYear,
#                 CarDetail.carColor,
#                 CarDetail.ownerName,
#                 CarDetail.registrationDoc,
#                 CarDetail.powerOfAttorneyDoc,
#                 CarDetail.registeredOn,
#                 CarDetail.carOwnedBySameVendor,
#                 CarDetail.CTD,
#                 CarTypeDetail.car_type
#             ).outerjoin(
#             DriverDetail, DriverDetail.DDID == Request.driverAssignedID
#             ).outerjoin(
#                 BidDetail, BidDetail.rID == Request.RID
#             ).outerjoin(
#                 CarDetail, CarDetail.CARID == BidDetail.CARID
#             ).outerjoin(
#                 CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
#             ).filter(Request.customerAppId == customer_app_id).order_by(Request.tableTimestamp.desc()).all()
#         if not requests:
#             return NoBidsResponse(message="NO REQUESTS FOUND")
#         return [
#                 RequestForUserResponse(
#                     REQUESTID=req.RID,
#                     FROMLOCATION=req.fromLocation,
#                     FROMLANDMARK=req.fromLandmark,
#                     TOLOCATION=req.toLocation,
#                     TOLANDMARK=req.toLandmark,
#                     PICKUPDATE=req.pickUpDate,                    
#                     PICKUPTIME=req.pickUpTime,
#                     NOOFADULTS=req.noOfAdults,
#                     NOOFKIDS=req.noOfKids,
#                     CARTYPE=req.carType,
#                     ACREQUEST=req.acRequest,
#                     CARRIERREQUES=req.carrierRequest,
#                     BIDENDTIME=req.bidEndTime,
#                     REQUESTSTATUS=req.requestStatus,
#                     PAYMENTSTATUS=req.paymentStatus,
#                     CUSTOMERAPPID=req.customerAppId,
#                     REQUESTWONBY=req.requestWonBy,
#                     FINALAMOUNT=req.finalAmount,
#                     NOOFBIDS=req.noOfBids,
#                     REJECTIONREASON=req.rejectionReason,
#                     REOPENBOOKING=req.requestReopened,
#                     TABLETIMESTAMP=req.tableTimestamp,
#                     REVIEWDONE=req.reviewDone,
#                     DRIVERNAME=driver_name,
#                     DRIVERNUMBER=driver_number,
#                     DRIVERPHOTO=driver_photo,
#                     DRIVERDOB=driver_dob,
#                     DRIVERGENDER=driver_gender,
#                     DRIVERCITY=driver_city,
#                     DRIVERLICENSE=driver_license,
#                     BIDAMOUNT=bid_amount,
#                     CARID=car_id,
#                     CARREGNO=car_reg_no,
#                     CARMODEL=car_model,
#                     MODELYEAR=model_year,
#                     CARCOLOR=car_color,
#                     OWNERNAME=owner_name,
#                     REGISTRATIONDOC=registration_doc,
#                     POWEROFATTORNEYDOC=power_of_attorney_doc,
#                     REGISTEREDON=registered_on,
#                     CAROWNEDBYSAMEVENDOR=car_owned_by_same_vendor,
#                     CTD=ctd,
#                     CAR_TYP=car_type

#                 )
#                     for req,driver_name,driver_number,driver_photo,driver_dob,
#                     driver_gender,driver_city,driver_license,bid_amount,car_id,car_reg_no,
#                      car_model,model_year,car_color,owner_name,registration_doc,
#                       power_of_attorney_doc,registered_on,car_owned_by_same_vendor,
#                        ctd,car_type in requests
#             ]
#     except SQLAlchemyError:
#         return NoBidsResponse(message="ERROR_PREPARE")
#     finally:
#         db.close()

def _history_flag_done(value: Optional[str]) -> bool:
    """Map request reviewDone / customerReviewDone Y/N-ish to bool."""
    if value is None:
        return False
    return str(value).strip().upper() == "Y"


def _history_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _history_model_year(value) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _history_pickup_datetime(req: Request) -> datetime:
    """Combine pickup date/time; fail safely on malformed historical values."""
    rid = getattr(req, "RID", None)
    pickup_date = getattr(req, "pickUpDate", None)
    pickup_time = getattr(req, "pickUpTime", None)
    if not isinstance(pickup_date, date):
        logger.error(
            "HISTORY_DATA_INVALID rid=%s reason=pickup_date_type",
            rid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )
    if not isinstance(pickup_time, time):
        logger.error(
            "HISTORY_DATA_INVALID rid=%s reason=pickup_time_type",
            rid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )
    try:
        d = pickup_date.date() if isinstance(pickup_date, datetime) else pickup_date
        return datetime.combine(d, pickup_time)
    except (TypeError, ValueError) as exc:
        logger.error(
            "HISTORY_DATA_INVALID rid=%s reason=pickup_combine",
            rid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        ) from exc


def _history_pickup_date_value(req: Request, pickup_dt: datetime) -> date:
    pickup_date = getattr(req, "pickUpDate", None)
    if isinstance(pickup_date, datetime):
        return pickup_date.date()
    if isinstance(pickup_date, date):
        return pickup_date
    return pickup_dt.date()


def _history_pickup_time_value(req: Request, pickup_dt: datetime) -> time:
    pickup_time = getattr(req, "pickUpTime", None)
    if isinstance(pickup_time, time):
        return pickup_time
    return pickup_dt.time()


def _assert_history_row_identity(req: Request) -> None:
    """Fail safely when required ownership/status/RID fields are unusable."""
    rid = getattr(req, "RID", None)
    if rid is None:
        logger.error("HISTORY_DATA_INVALID rid=None reason=missing_rid")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )
    status_value = getattr(req, "requestStatus", None)
    if not status_value or not str(status_value).strip():
        logger.error(
            "HISTORY_DATA_INVALID rid=%s reason=missing_status",
            rid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )


def _history_cancellation_reason(value) -> str:
    """Map rejectionReason for cancellation history; never return literal null."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "null":
        return ""
    return text


def _history_nullable_final_amount(value) -> Optional[float]:
    """Preserve null; do not coerce null to zero."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _history_required_location(value, *, rid, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() == "null":
        logger.error(
            "HISTORY_DATA_INVALID rid=%s reason=missing_%s",
            rid,
            field,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )
    return text


def _history_required_customer_name(value, *, rid) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() == "null":
        logger.error(
            "HISTORY_DATA_INVALID rid=%s reason=missing_customer_name",
            rid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_DATA_INVALID",
        )
    return text


def get_all_requests_for_user(
    db: Session,
    user_id: str,
    customer_app_id: Optional[str] = None,
) -> List[CustomerBookingHistoryItem]:
    """
    Customer completed booking history (PR20).

    JWT sub is authoritative. Optional transitional customerAppId must match
    JWT or returns 403. Returns only past REQUEST - CONFIRMED rows owned by
    JWT, newest pickup first (RID desc tie-break). Empty → [].
    """
    if not user_id or not str(user_id).strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    owner_id = str(user_id).strip()

    if customer_app_id is not None and str(customer_app_id).strip():
        if str(customer_app_id).strip() != owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized",
            )

    try:
        rows = (
            db.query(
                Request,
                DriverDetail.driverName,
                DriverDetail.driverPhoto,
                DriverDetail.driverDOB,
                DriverDetail.driverGender,
                CarDetail.carRegNo,
                CarDetail.carModel,
                CarDetail.modelYear,
            )
            .outerjoin(
                DriverDetail, DriverDetail.DDID == Request.driverAssignedID
            )
            .outerjoin(
                BidDetail,
                (BidDetail.rID == Request.RID)
                & (BidDetail.bidderID == Request.requestWonBy),
            )
            .outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID)
            .filter(
                Request.customerAppId == owner_id,
                Request.requestStatus == _CONFIRMED_STATUS,
            )
            .all()
        )
    except SQLAlchemyError:
        logger.exception(
            "get_all_requests_for_user query failed owner_hash=%s",
            hash(owner_id) % 100000,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_QUERY_FAILED",
        )

    now_ist = _now_ist_naive()
    items: List[tuple] = []
    for (
        req,
        driver_name,
        driver_photo,
        driver_dob,
        driver_gender,
        car_reg_no,
        car_model,
        model_year,
    ) in rows:
        _assert_history_row_identity(req)
        if getattr(req, "customerAppId", None) != owner_id:
            logger.error(
                "HISTORY_DATA_INVALID rid=%s reason=ownership_mismatch",
                getattr(req, "RID", None),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="HISTORY_DATA_INVALID",
            )
        pickup_dt = _history_pickup_datetime(req)
        if pickup_dt >= now_ist:
            continue
        items.append(
            (
                pickup_dt,
                int(req.RID),
                CustomerBookingHistoryItem(
                    requestId=int(req.RID),
                    requestStatus=str(req.requestStatus),
                    fromLocation=req.fromLocation or "",
                    toLocation=req.toLocation or "",
                    pickupDate=_history_pickup_date_value(req, pickup_dt),
                    pickupTime=_history_pickup_time_value(req, pickup_dt),
                    noOfAdults=int(req.noOfAdults or 0),
                    noOfKids=int(req.noOfKids or 0),
                    carType=req.carType or "",
                    acRequested=bool(req.acRequest),
                    carrierRequested=bool(req.carrierRequest),
                    specialRequest=_history_optional_text(req.specialRequest),
                    reviewDone=_history_flag_done(req.reviewDone),
                    driverName=_history_optional_text(driver_name),
                    driverProfileImageUrl=_history_optional_text(driver_photo),
                    driverGender=_history_optional_text(driver_gender),
                    driverDateOfBirth=driver_dob
                    if isinstance(driver_dob, date)
                    else None,
                    carRegistrationNumber=_history_optional_text(car_reg_no),
                    carModel=_history_optional_text(car_model),
                    modelYear=_history_model_year(model_year),
                ),
            )
        )

    items.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in items]


def get_rid_by_details(db : Session, from_location : str, to_location : str, pick_up_date : str, 
                       pick_up_time : str, no_of_adults : int, no_of_kids : int, car_type : str):
    
    try:
        pick_up_date = datetime.strptime(pick_up_date, '%Y-%m-%d').date()
        pick_up_time = datetime.strptime(pick_up_time,'%H:%M:%S').time()
    except ValueError:
        return NoBidsResponse(message="INVALID DATE OR TIME FORMAT")
    try :
        requests = (
            db.query(Request).
            filter(
                Request.fromLocation == from_location,
                Request.toLocation == to_location,
                Request.pickUpDate == pick_up_date,
                Request.pickUpTime == pick_up_time,
                Request.noOfAdults == no_of_adults,
                Request.noOfKids == no_of_kids,
                Request.carType == car_type
            ).
            order_by(Request.RID.desc())
            .first()
        )

        if not requests:
            return NoBidsResponse(message="NO REQUEST FOUND")
        
        return RequestByRidResponse(RID=requests.RID)
    except SQLAlchemyError:
        return NoBidsResponse(message="ERROR_PREPARE")
    finally:
        db.close()


def get_booking_report(db:Session, start_date : str, end_date :str):
    try:
        start_date = datetime.strptime(start_date,'%Y-%m-%d').date()
        end_date = datetime.strptime(end_date,'%Y-%m-%d').date()
    except ValueError:
        return NoBidsResponse(message="INVALID DATE OR TIME")
    
    try:
        requests = db.query(Request).filter(
            Request.pickUpDate.between(start_date, end_date) 
        ).order_by(
            Request.pickUpDate.asc(),
            Request.pickUpTime.asc(),
            Request.tableTimestamp.asc()
        ).all()

        if not requests:
            return NoBidsResponse(message="NO REQUESTS FOUND")

        return [
            GetBookingReportResponse(
                REQUESTID=req.RID,
                WIZZPNR=req.WIZZPNR,
                FROMLOCATION=req.fromLocation,
                FROMLANDMARK=req.fromLandmark,
                TOLOCATION=req.toLocation,
                TOLANDMARK=req.toLandmark,
                PICKUPDATE=req.pickUpDate,
                PICKUPTIME=req.pickUpTime,
                NOOFADULTS=req.noOfAdults,
                NOOFKIDS=req.noOfKids,
                CARTYPE=req.carType,
                ACREQUEST="Required" if req.acRequest else "Not Required",
                CARRIERREQUEST="Required" if req.carrierRequest else "Not Required",
                BIDENDTIME=req.bidEndTime.strftime("%d-%m-%Y %H:%M:%S") if req.bidEndTime else None,
                REQUESTSTATUS=req.requestStatus,
                CUSTOMERAPPID=req.customerAppId,
                REQUESTWONBY=req.requestWonBy,
                FINALAMOUNT=req.finalAmount,
                NOOFBIDS=req.noOfBids,
                REJECTIONREASON=req.rejectionReason,
                REQUESTOPENED=req.requestReopened,
                REVIEWDONE=req.reviewDone,
                TABLETIMESTAMP=req.tableTimestamp.strftime("%d-%m-%Y %H:%M:%S") if req.tableTimestamp else None,
        ) for req in requests]
    except SQLAlchemyError:
        return NoBidsResponse(message="ERROR_PREPARE")
    finally:
        db.close()

# This will fetch all the Requests that the vendor has bid on and the status is either BID - OPEN OR BID - CONFIRMED
def get_all_open_requests_for_vendor(db: Session, vendor_id : int):
    try:
        requests = db.query(Request).join(
            BidDetail, BidDetail.rID == Request.RID
        ).filter(
            (Request.requestStatus.in_(["BID - OPEN","BID - CONFIRMED"])) &
            (BidDetail.bidderID == vendor_id) 
        ).all()

        if not requests:
            return NoBidsResponse(message="NO REQUESTS FOUND")
        
        return [RequestConfirmedCommonResponse(
                REQUESTID=req.RID,
                FROMLOCATION=req.fromLocation,
                FROMLANDMARK=req.fromLandmark,
                TOLOCATION=req.toLocation,
                TOLANDMARK=req.toLandmark,
                PICKUPDATE=req.pickUpDate,
                PICKUPTIME=req.pickUpTime,
                NOOFADULTS=req.noOfAdults,
                NOOFKIDS=req.noOfKids,
                CARTYPE=req.carType,
                ACREQUEST=req.acRequest,
                CARRIERREQUES=req.carrierRequest,
                BIDENDTIME=req.bidEndTime,
                REQUESTSTATUS=req.requestStatus,
                PAYMENTSTATUS=req.paymentStatus,
                CUSTOMERAPPID=req.customerAppId,
                REQUESTWONBY=req.requestWonBy,
                NOOFBIDS=req.noOfBids,
                TABLETIMESTAMP=req.tableTimestamp
            ) for req in requests]
    except SQLAlchemyError:
        return NoBidsResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    
def get_request_type(db:Session):
    try:
        types = db.query(RequestType).all()
        return [RequestTypeBase.model_validate(type) for type in types]
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    

def delete_request(
    db: Session,
    r_id: int,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = None,
):
    """
    Soft-cancel a customer request (requestStatus → REQUEST - CANCELLED BY USER).

    When ``user_id`` is provided (JWT ``sub`` from DELETE /deleterequest):
    ownership and BID - OPEN status are enforced before mutation.
    """
    try:
        existing = db.query(Request).filter(Request.RID == r_id).first()

        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if user_id is not None and existing.customerAppId != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this request",
            )

        if existing.requestStatus != "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        updated = db.query(Request).filter(Request.RID == r_id).update(
            {Request.requestStatus: "REQUEST - CANCELLED BY USER"}
        )
        db.commit()

        if updated == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        # Background task opens its own SessionLocal — do not pass request db.
        background_tasks.add_task(
            notify_vendors_request_cancelled,
            r_id,
        )

        return ErrorResponse(message="DELETED")
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[delete_request] ERROR: {e}")
        return ErrorResponse(message="DELETED ERROR IN FUNCTION")
    finally:
        db.close()


def update_request(
    db: Session,
    request_data: RequestUpdate,
    user_id: Optional[str] = None,
):
    """
    Update editable fields on a customer request.

    When ``user_id`` is provided (JWT ``sub`` from PUT /updaterequest):
    ownership and BID - OPEN status are enforced before mutation.
    Validation order: exists → ownership → BID - OPEN → noOfBids → update.
    """
    try:
        existing = db.query(Request).filter(Request.RID == request_data.RID).first()

        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if user_id is not None and existing.customerAppId != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to update this request",
            )

        if existing.requestStatus != "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if (existing.noOfBids or 0) > 0:
            return ErrorResponse(message="NO OF BIDS MORE THAN 0")

        special = (
            request_data.specialRequest.strip()
            if request_data.specialRequest and request_data.specialRequest.strip()
            else None
        )

        updated = db.query(Request).filter(Request.RID == request_data.RID).update({
            Request.fromLocation: request_data.fromLocation,
            Request.fromLandmark: request_data.fromLandmark,
            Request.toLocation: request_data.toLocation,
            Request.toLandmark: request_data.toLandmark,
            Request.pickUpDate: request_data.pickUpDate,
            Request.pickUpTime: request_data.pickUpTime,
            Request.noOfAdults: request_data.noOfAdults,
            Request.noOfKids: request_data.noOfKids,
            Request.carType: request_data.carType,
            Request.acRequest: 1 if request_data.acRequest else 0,
            Request.carrierRequest: 1 if request_data.carrierRequest else 0,
            Request.specialRequest: special,
            Request.bidEndTime: request_data.bidEndTime,
            Request.tableTimestamp: datetime.now(
                ZoneInfo("Asia/Kolkata")
            ).replace(tzinfo=None),
        })
        db.commit()

        if updated == 0:
            return ErrorResponse(message="FAILED")

        return ErrorResponse(message="SUCCESS")

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[update_request] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[update_request] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
# def accept_by_vendor(db: Session, rid : int, vendor_id : int, final_amount : float):
#     try:
#         requestupdate = db.query(Request).filter(Request.RID == rid).update({
#             Request.requestStatus: "REQUEST - CONFIRMED",
#             Request.requestWonBy: vendor_id,
#             Request.finalAmount:final_amount,
#             Request.tableTimestamp:func.current_timestamp()

#         })
#         db.commit()

#         if requestupdate==0:
#             return ErrorResponse(message="REQUEST TABLE UPDATE FAILED")
        
#         bidupdate = db.query(BidDetail).filter((BidDetail.rID == rid)&(BidDetail.bidderID == vendor_id)).update({
#             BidDetail.bidStatus: "BID - CONFIRMED",
#             BidDetail.tableTimestamp: func.current_timestamp()
#         })
#         db.commit()

#         if bidupdate==0:
#             return ErrorResponse(message="BID UPDATE STATUS FAILED")
        
#         return ErrorResponse(message="UPDATED")
#     except SQLAlchemyError:
#         db.rollback()
#         return ErrorResponse(message="ERROR") 
#     finally:
#         db.close()

def accept_by_vendor(db: Session, rid: int, vendor_id: int, final_amount: float,bid_id : int = None, car_id : int = None):
    try:
        with db.begin():
            # =========================
            # 1) UPDATE REQUEST
            # =========================
            request_update = db.query(Request).filter(Request.RID == rid).update({
                Request.requestStatus: "REQUEST - CONFIRMED",
                Request.requestWonBy: vendor_id,
                Request.finalAmount: final_amount,
                Request.tableTimestamp: func.current_timestamp()
            })

            if request_update == 0:
                db.rollback()
                return ErrorResponse(message="REQUEST UPDATE FAILED")
            
            # =========================
            # 2) UPDATE BID (PHP LOGIC)
            # =========================

            if bid_id: 
                bid_query = db.query(BidDetail).filter(
                    BidDetail.BID == bid_id,
                    BidDetail.rID == rid,
                    BidDetail.bidderID == vendor_id
                )
            
            elif car_id:
                bid_query = db.query(BidDetail).filter(
                    BidDetail.rID == rid,
                    BidDetail.bidderID == vendor_id,
                    BidDetail.CARID == car_id
                )   

            else : 
                # fallback → latest bid
                latest_bid = db.query(BidDetail).filter(
                    BidDetail.rID == rid,
                    BidDetail.bidderID == vendor_id
                ).order_by(BidDetail.tableTimestamp.desc()).first()

                if not latest_bid:
                    db.rollback()
                    return ErrorResponse(message="NO BID FOUND")
                
                bid_query = db.query(BidDetail).filter(BidDetail.BID == latest_bid.BID)

            updated = bid_query.update({
                BidDetail.bidStatus: "REQUEST - CONFIRMED",
                BidDetail.tableTimestamp: func.current_timestamp()
            })

            if updated == 0:
                db.rollback()
                return ErrorResponse(message="BID UPDATE FAILED")
            
            # fetch customer id before commit so we can notify later
            request_row = db.query(Request.customerAppId).filter(Request.RID == rid).first()
            customer_user_app_id = request_row.customerAppId if request_row else None

            # =========================
            # 3) NOTIFY LOSING VENDORS
            # =========================
            try : 
                losing_vendor_ids = get_other_vendors_who_bid_on_request(
                    db=db,
                    rid=rid,
                    excluded_vendor_id=vendor_id
                )

                if losing_vendor_ids:
                    notify_data = FCMSendDrivers(
                        title="The request was won by another vendor!",
                        body="The request was won by another vendor!",
                        url="The request was won by another vendor!",
                        soundFile="normal_notification",
                        driverIds=losing_vendor_ids
                    )

                    send_notification_to_selected_users(db, notify_data)
            except Exception:
                pass



            # =========================
            # 4) NOTIFY CUSTOMER
            # =========================

            try:
                if customer_user_app_id:
                    send_notification_to_user(
                        db,
                        FCMSend(
                            userAppId=customer_user_app_id,
                            title="Booking Confirmed!",
                            body="Vendor has accepted your request!",
                            url="Booking Confirmed!",
                            type="passengernotification",
                            soundFile="normal_notification",
                            source="",
                            destination="",
                            travelDate="",
                            pickupTime="",
                        ),
                    )
            except Exception:
                pass

            # =========================
            # 5) NOTIFY WINNING VENDOR
            # =========================
            try:
                send_notification_to_user(
                    db,
                    FCMSend(
                        userAppId=vendor_id,
                        title="Trip Confirmed!",
                        body="Your Trip has been Confirmed.",
                        url="Trip Confirmed!",
                        type="passengernotification",
                        soundFile="normal_notification",
                        source="",
                        destination="",
                        travelDate="",
                        pickupTime="",
                    ),
                )
            except Exception:
                pass

            return ErrorResponse(message="UPDATED")

    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="ERROR", error=str(e))
                        



# def reject_request_by_vendor(db:Session, rid :int , bid_id : int, rejection_reason : str):
#     try:
#         update = db.query(Request).filter(Request.RID == rid).update({
#             Request.requestStatus:"BID - OPEN",
#             Request.rejectionReason:rejection_reason,
#             Request.tableTimestamp:func.current_timestamp()
#         })
#         if update==0:
#             db.rollback()
#             return ErrorResponse(message="REQUEST TABLE UPDATE FAILED")
        
#         delete_bid = delete_bid_with_bid(db,rid=rid,bid=bid_id)
        
#         if delete_bid.message != 'DELETED':
#             db.rollback()
#             return ErrorResponse(message="REQUEST UPDATED BUT BID NOT DELETED")
#         db.commit()
#         return ErrorResponse(message="UPDATED")
#     except SQLAlchemyError:
#         db.rollback()
#         return ErrorResponse(message="ERROR")
#     finally:
#         db.close()

# def reject_request_by_vendor(
#     db: Session,
#     rid: int,
#     bid_id: int,
#     rejection_reason: str
# ) -> EmailErrorResponse | ErrorResponse:
#     try:
#         # One atomic transaction: if any step fails, nothing is saved
#         with db.begin():
#             # 1) Delete the bid (must match both BID and rID)
#             deleted = (
#                 db.query(BidDetail)
#                   .filter(BidDetail.BID == bid_id, BidDetail.rID == rid)
#                   .delete(synchronize_session=False)
#             )
#             if deleted == 0:
#                 # Raising inside `with db.begin()` will auto-rollback
#                 raise ValueError("NO ROWS DELETED")

#             # 2) Recompute accurate noOfBids for this request (safer than decrement)
#             new_count = (
#                 db.query(func.count(BidDetail.BID))
#                   .filter(BidDetail.rID == rid)
#                   .scalar()
#             )

#             # 3) Update the request row only if delete succeeded
#             updated = (
#                 db.query(Request)
#                   .filter(Request.RID == rid)
#                   .update(
#                       {
#                           Request.noOfBids: new_count,
#                           Request.requestStatus: "BID - OPEN",
#                           Request.rejectionReason: rejection_reason,
#                           Request.tableTimestamp: func.current_timestamp(),
#                       },
#                       synchronize_session=False,
#                   )
#             )
#             if updated == 0:
#                 raise ValueError("REQUEST TABLE UPDATE FAILED")

#         # If we reached here, the transaction committed successfully
#         return EmailErrorResponse(message="UPDATED")

#     except ValueError as ve:
#         # Transaction already rolled back by context manager
#         return ErrorResponse(message=str(ve))

#     except SQLAlchemyError as e:
#         # Any DB error → rollback (context manager handles it), return error
#         return ErrorResponse(message="ERROR")

def reject_request_by_vendor(
    db: Session,
    rid: int,
    bidder_id: str,
    bid_id : int,
    rejection_reason: str,
    notification_type: str = "default",
) -> EmailErrorResponse | ErrorResponse:
    try:
        customer_app_id = None
        with db.begin():
            # 1) Fetch request first
            request_row = db.query(Request).filter(Request.RID == rid).first()
            if not request_row:
                return ErrorResponse(message="REQUEST NOT FOUND")
            
            customer_app_id = request_row.customerAppId

            # 2) Load exact bid row to verify it belongs to this request
            bid_row = (
                db.query(BidDetail)
                .filter(BidDetail.BID == bid_id, BidDetail.rID == rid)
                .first()
            )
            if not bid_row:
                return ErrorResponse(message="BID NOT FOUND")

            # 3) Update request
            updated = (
                db.query(Request)
                .filter(Request.RID == rid)
                .update(
                    {
                        Request.requestStatus: "BID - OPEN",
                        Request.rejectionReason: rejection_reason,
                        Request.tableTimestamp: func.current_timestamp(),
                    },
                    synchronize_session=False,
                )
            )

            if updated == 0:
                return ErrorResponse(message="INSERT ERROR IN FUNCTION")
            
            # 4) Delete ONLY the exact bid row
            deleted = (
                db.query(BidDetail)
                .filter(BidDetail.BID == bid_id, BidDetail.rID == rid)
                .delete(synchronize_session=False)
            )
            if deleted == 0:
                return ErrorResponse(message="REQUEST UPDATED BUT BID NOT DELETED")
            
            # 5) Recompute noOfBids
            new_count = (
                db.query(func.count(BidDetail.BID))
                .filter(BidDetail.rID == rid)
                .scalar()
            ) or 0
            db.query(Request).filter(Request.RID == rid).update(
                {Request.noOfBids: new_count,
                 Request.tableTimestamp: func.current_timestamp()
                },
                synchronize_session=False
            )

        # 6) Notify remaining vendors
        try:
            remaining_vendor_ids = get_vendors_who_bid_on_request(db, rid)

            if remaining_vendor_ids:
                send_notification_to_selected_users(
                    db,
                    FCMSendDrivers(
                        title="Bidding Reopened!",
                        body="⏳ The request has been opened again! 📂",
                        url="Bidding Reopened!",
                        soundFile="alarm_notification",
                        driverIds=remaining_vendor_ids,
                    ),
                )
        except Exception:
            pass

         # 7) Notify customer
        try:
            if customer_app_id:
                send_notification_to_user(
                    db,
                    FCMSend(
                        userAppId=customer_app_id,
                        title="Vendor Rejected your Request!",
                        body=rejection_reason,
                        url="Vendor Rejected your Request!",
                        type=notification_type,
                        soundFile="normal_notification",
                        source="",
                        destination="",
                        travelDate="",
                        pickupTime="",
                    ),
                )
        except Exception:
            pass
        
        return EmailErrorResponse(message="UPDATED")

    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message="ERROR", error=str(e))

            
    
def cancel_handshake(
    db: Session,
    rid: int,
    user_id: Optional[str] = None,
    actor_auth_subject: Optional[str] = None,
):
    """
    Customer cancel handshake (PR10).

    BID - CONFIRMED → BID - OPEN (request + all bids) in one transaction.
    BID - OPEN → idempotent CANCELLED (repair bids to BID - OPEN if needed).
    Other statuses → 409. No FCM in PR10.
    Does not modify requestWonBy / finalAmount (typically unset at handshake).

    PR40: emit handshake.cancelled only on real BID - CONFIRMED → BID - OPEN.
    Already-open repair may still commit but creates no event.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        request_row = (
            db.query(Request)
            .filter(Request.RID == rid)
            .with_for_update()
            .first()
        )

        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if user_id is not None and request_row.customerAppId != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to cancel handshake for this request",
            )

        now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)

        if request_row.requestStatus == "BID - OPEN":
            # Idempotent: already reopened. Repair bid statuses if needed.
            # No domain event (D8) — preserve repair business behaviour.
            db.query(BidDetail).filter(BidDetail.rID == rid).update(
                {
                    BidDetail.bidStatus: "BID - OPEN",
                    BidDetail.tableTimestamp: now,
                },
                synchronize_session=False,
            )
            db.commit()
            return ErrorResponse(message="CANCELLED")

        if request_row.requestStatus != "BID - CONFIRMED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        request_updated = (
            db.query(Request)
            .filter(Request.RID == rid)
            .update(
                {
                    Request.requestStatus: "BID - OPEN",
                    Request.tableTimestamp: now,
                },
                synchronize_session=False,
            )
        )
        if request_updated == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        # requestWonBy / finalAmount intentionally unchanged.
        db.query(BidDetail).filter(BidDetail.rID == rid).update(
            {
                BidDetail.bidStatus: "BID - OPEN",
                BidDetail.tableTimestamp: now,
            },
            synchronize_session=False,
        )

        # B2 decision marker (no RID). Prove gate + transition around append.
        previous_status = "BID - CONFIRMED"
        appended = maybe_append_domain_event(
            db,
            event_type=EVENT_HANDSHAKE_CANCELLED,
            aggregate_id=str(rid),
            payload={"requestId": int(rid)},
            actor_auth_subject=actor_auth_subject,
        )
        log_handshake_cancelled_emission_decision(
            previous_status=previous_status,
            transition_eligible=True,
            append_attempted=True,
            append_succeeded=appended is not None,
        )

        db.commit()
        return ErrorResponse(message="CANCELLED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[cancel_handshake] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[cancel_handshake] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    

# def booking_cancelled_by_user(db:Session, rid : int, rejection_reason : str):
#     try:
#         update = db.query(Request).filter(Request.RID == rid).update({
#             Request.requestStatus:"BOOKING - CANCELLED BY USER'",
#             Request.rejectionReason:rejection_reason,
#             Request.tableTimestamp:func.current_timestamp()
#         })
#         db.commit()
#         if update==0:
#             return ErrorResponse(message="REQUEST TABLE UPDATE FAILED")        
#         return ErrorResponse(message="UPDATED")
#     except SQLAlchemyError:
#         db.rollback()
#         return ErrorResponse(message="ERROR")
#     finally:
#         db.close()


def _now_ist_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def _request_pickup_datetime(request_row: Request) -> datetime:
    return datetime.combine(request_row.pickUpDate, request_row.pickUpTime)


def _validate_cancellation_reason(rejection_reason: Optional[str]) -> str:
    """Trim and validate cancellation reason. Raises HTTP 422 on invalid input."""
    if rejection_reason is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid cancellation reason",
        )
    trimmed = rejection_reason.strip()
    if not trimmed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid cancellation reason",
        )
    if len(trimmed) > _REJECTION_REASON_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid cancellation reason",
        )
    return trimmed


def booking_cancelled_by_user(
    db: Session,
    rid: int,
    rejection_reason: str,
    user_id: Optional[str] = None,
    background_tasks: Optional[BackgroundTasks] = None,
    actor_auth_subject: Optional[str] = None,
):
    """
    Customer confirmed-booking cancellation (PR12).

    REQUEST - CONFIRMED → BOOKING - CANCELLED BY USER.
    JWT sub is authoritative owner. Vendor notify recipient = request.requestWonBy.
    Preserves requestWonBy, finalAmount, bids, driver, and payment fields.

    PR40: emit booking.cancelled_by_customer only on real status transition.
    """
    try:
        request_row = (
            db.query(Request)
            .filter(Request.RID == rid)
            .with_for_update()
            .first()
        )

        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if user_id is not None and request_row.customerAppId != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to cancel this booking",
            )

        # Idempotent replay: already cancelled by user → 200 UPDATED, no re-notify, no event.
        if request_row.requestStatus == STATUS_BOOKING_CANCELLED_BY_USER:
            db.commit()
            return ErrorResponse(message="UPDATED")

        if request_row.requestStatus != STATUS_REQUEST_CONFIRMED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        now = _now_ist_naive()
        pickup_dt = _request_pickup_datetime(request_row)
        if pickup_dt <= now:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="CANCELLATION_NOT_ALLOWED",
            )

        trimmed_reason = _validate_cancellation_reason(rejection_reason)

        vendor_to_notify = (
            str(request_row.requestWonBy).strip()
            if request_row.requestWonBy
            else None
        )

        updated = (
            db.query(Request)
            .filter(Request.RID == rid)
            .update(
                {
                    Request.requestStatus: STATUS_BOOKING_CANCELLED_BY_USER,
                    Request.rejectionReason: trimmed_reason,
                    Request.tableTimestamp: now,
                },
                synchronize_session=False,
            )
        )
        if updated == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        # Never put rejectionReason in the event payload.
        maybe_append_domain_event(
            db,
            event_type=EVENT_BOOKING_CANCELLED_BY_CUSTOMER,
            aggregate_id=str(rid),
            payload={"requestId": int(rid)},
            actor_auth_subject=actor_auth_subject,
        )

        db.commit()

        if background_tasks is not None and vendor_to_notify:
            background_tasks.add_task(
                notify_vendor_booking_cancelled_by_customer,
                vendor_to_notify,
            )

        return ErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[booking_cancelled_by_user] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[booking_cancelled_by_user] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()


def get_all_confirmed_requests_for_customer(db: Session, user_app_id : str):
    try:
        with db.begin():
            confirmed_requests = db.query(Request,
                                          User.fullName,
                                          User.city,
                                          User.userAppId,
                                          User.alternateNumber,
                                          User.rating,
                                          User.totalNoOfReviews
                                          ).join(
                User, User.userAppId == Request.requestWonBy
            ).filter(
                (Request.requestStatus == "REQUEST - CONFIRMED") &
                (User.userAppId == user_app_id)
            ).all()

            return [RequestConfirmedForUserResponse(
                REQUESTID=requests.RID,
                FROMLOCATION=requests.fromLocation,
                FROMLANDMARK=requests.fromLandmark,                
                TOLOCATION=requests.toLocation,
                TOLANDMARK=requests.toLandmark,
                PICKUPDATE=requests.pickUpDate,
                PICKUPTIME=requests.pickUpTime,
                NOOFADULTS=requests.noOfAdults,
                NOOFKIDS=requests.noOfKids,
                CARTYPE=requests.carType,
                ACREQUEST=requests.acRequest,
                CARRIERREQUES=requests.carrierRequest,
                BIDENDTIME=requests.bidEndTime,
                REQUESTSTATUS=requests.requestStatus,
                PAYMENTSTATUS=requests.paymentStatus,
                CUSTOMERAPPID=requests.customerAppId,
                REQUESTWONBY=requests.requestWonBy,
                FINALAMOUNT=requests.finalAmount,
                VENDORNAME=full_name,
                VENDORCITY=city,
                VENDORNUMBER=user_ap_id,
                VENDORALTNUMBER=alternat_number,
                VENDORRATING=rating,
                VENDORTOTALREVIEWS=total_no_of_reviews
            ) for requests,full_name,city,user_ap_id,alternat_number,rating,total_no_of_reviews in confirmed_requests
        ]
    except SQLAlchemyError as e : 
        db.rollback()
        return EmailErrorResponse(message="ERROR",error=str(e))
    finally:
        db.close()
    

def get_all_confirmed_requests_for_vendor(
    db: Session,
    user_id: str,
    vendor_id: Optional[str] = None,
) -> List[VendorBookingHistoryItem]:
    """
    Vendor completed trip history (PR20).

    JWT sub is authoritative via requestWonBy. Optional transitional vendorId
    must match JWT or returns 403. Past REQUEST - CONFIRMED only, newest
    pickup first (RID desc tie-break). Empty → [].
    Losing bidders never receive rows.
    """
    if not user_id or not str(user_id).strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    owner_id = str(user_id).strip()

    if vendor_id is not None and str(vendor_id).strip():
        if str(vendor_id).strip() != owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized",
            )

    try:
        confirmed_requests = (
            db.query(
                Request,
                User.fullName,
                User.profilePicture,
                CustomerReview.generalRating,
                CustomerReview.comments,
                DriverDetail.driverName,
                CarDetail.carRegNo,
                CarDetail.carModel,
            )
            .join(User, User.userAppId == Request.customerAppId)
            .outerjoin(CustomerReview, CustomerReview.RID == Request.RID)
            .outerjoin(
                DriverDetail, DriverDetail.DDID == Request.driverAssignedID
            )
            .outerjoin(
                BidDetail,
                (BidDetail.rID == Request.RID)
                & (BidDetail.bidderID == Request.requestWonBy),
            )
            .outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID)
            .filter(
                Request.requestStatus == _CONFIRMED_STATUS,
                Request.requestWonBy == owner_id,
            )
            .all()
        )
    except SQLAlchemyError:
        logger.exception(
            "get_all_confirmed_requests_for_vendor query failed owner_hash=%s",
            hash(owner_id) % 100000,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_QUERY_FAILED",
        )

    now_ist = _now_ist_naive()
    items: List[tuple] = []
    for (
        req,
        full_name,
        profile_picture,
        general_rating,
        review_comments,
        driver_name,
        car_reg_no,
        car_model,
    ) in confirmed_requests:
        _assert_history_row_identity(req)
        if getattr(req, "requestWonBy", None) != owner_id:
            logger.error(
                "HISTORY_DATA_INVALID rid=%s reason=won_by_mismatch",
                getattr(req, "RID", None),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="HISTORY_DATA_INVALID",
            )
        pickup_dt = _history_pickup_datetime(req)
        if pickup_dt >= now_ist:
            continue

        rating_value: Optional[float] = None
        if general_rating is not None:
            try:
                rating_value = float(general_rating)
            except (TypeError, ValueError):
                rating_value = None

        items.append(
            (
                pickup_dt,
                int(req.RID),
                VendorBookingHistoryItem(
                    requestId=int(req.RID),
                    requestStatus=str(req.requestStatus),
                    fromLocation=req.fromLocation or "",
                    toLocation=req.toLocation or "",
                    pickupDate=_history_pickup_date_value(req, pickup_dt),
                    pickupTime=_history_pickup_time_value(req, pickup_dt),
                    noOfAdults=int(req.noOfAdults or 0),
                    noOfKids=int(req.noOfKids or 0),
                    carType=req.carType or "",
                    acRequested=bool(req.acRequest),
                    carrierRequested=bool(req.carrierRequest),
                    specialRequest=_history_optional_text(req.specialRequest),
                    finalAmount=float(req.finalAmount or 0),
                    customerDisplayName=(full_name or "").strip() or "Passenger",
                    customerProfileImageUrl=_history_optional_text(
                        profile_picture
                    ),
                    customerReviewDone=_history_flag_done(
                        req.customerReviewDone
                    ),
                    customerGeneralRating=rating_value,
                    customerReviewComments=_history_optional_text(
                        review_comments
                    ),
                    carRegistrationNumber=_history_optional_text(car_reg_no),
                    carModel=_history_optional_text(car_model),
                    driverName=_history_optional_text(driver_name),
                ),
            )
        )

    items.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in items]


def reopen_request(
    db: Session,
    r_id: int,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = None,
):
    """
    Reopen a cancelled booking (PR12).

    Marks original requestReopened=1 and clones a new BID - OPEN request
    with the same pickup datetime and bidEndTime (must both still be future).
    Does not mutate the original status away from BOOKING - CANCELLED BY USER.
    """
    try:
        original = (
            db.query(Request)
            .filter(Request.RID == r_id)
            .with_for_update()
            .first()
        )

        if not original:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if user_id is not None and original.customerAppId != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to reopen this booking",
            )

        if bool(original.requestReopened):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="REQUEST_ALREADY_REOPENED",
            )

        if original.requestStatus != STATUS_BOOKING_CANCELLED_BY_USER:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        now = _now_ist_naive()
        pickup_dt = _request_pickup_datetime(original)
        if pickup_dt <= now:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="REOPEN_NOT_ALLOWED",
            )

        if original.bidEndTime is None or original.bidEndTime <= now:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="REOPEN_NOT_ALLOWED",
            )

        create_data = RequestCreate(
            fromLocation=original.fromLocation,
            fromLandmark=original.fromLandmark,
            toLocation=original.toLocation,
            toLandmark=original.toLandmark,
            pickUpDate=original.pickUpDate,
            pickUpTime=original.pickUpTime,
            noOfAdults=original.noOfAdults,
            noOfKids=original.noOfKids,
            carType=original.carType,
            acRequest=bool(original.acRequest),
            carrierRequest=bool(original.carrierRequest),
            specialRequest=original.specialRequest,
            bidEndTime=original.bidEndTime,
            customerAppId=original.customerAppId,
            requestType=original.requestType,
            wizzpnr=original.WIZZPNR,
        )

        insert_result = insert_request_row(
            db,
            create_data,
            user_id=user_id if user_id is not None else original.customerAppId,
            commit=False,
            close_session=False,
            notify=False,
        )

        if isinstance(insert_result, EmailErrorResponse):
            db.rollback()
            return ReopenBookingResponse(
                message=insert_result.message,
                error=getattr(insert_result, "error", None),
            )

        new_request = insert_result

        original.requestReopened = True
        original.tableTimestamp = now

        db.commit()

        # Notify eligible vendors after commit (same as normal create).
        try:
            vendor_ids = get_vendors_for_request(
                db,
                create_data.fromLocation,
                create_data.toLocation,
            )
            if vendor_ids:
                background_tasks.add_task(
                    notify_vendors_for_request,
                    vendor_ids,
                    create_data,
                )
        except Exception as e:
            print(f"[reopen_request] notify schedule error: {e}")

        return ReopenBookingResponse(
            message="UPDATED",
            newRequestId=int(new_request.RID),
        )

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[reopen_request] ERROR: {e}")
        return ReopenBookingResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[reopen_request] ERROR_EXCEPTION: {e}")
        return ReopenBookingResponse(message="ERROR")
    finally:
        db.close()


def insert_request_row(
    db: Session,
    create_data: RequestCreate,
    *,
    user_id: Optional[str] = None,
    commit: bool = True,
    close_session: bool = True,
    notify: bool = True,
    background_tasks: Optional[BackgroundTasks] = None,
    emit: bool = True,
):
    """
    Validate and insert a new request row.

    When ``commit=False``, the row is flushed onto the shared session without
    committing or closing — safe for reopen's outer transaction.
    When ``commit=True`` (default), behaves like the public create_request path.
    """
    try:
        if user_id is not None:
            body_customer = (create_data.customerAppId or "").strip()
            if not body_customer or body_customer != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Customer identity does not match authenticated user",
                )
            persisted_customer_id = user_id
        else:
            persisted_customer_id = create_data.customerAppId

        request_status = STATUS_BID_OPEN
        ac_request = bool(create_data.acRequest)
        carrier_request = bool(create_data.carrierRequest)
        request_type = create_data.requestType if create_data.requestType else 1
        wizzpnr = create_data.wizzpnr if create_data.wizzpnr else None

        existing_customer = db.query(User).filter(
            User.userAppId == persisted_customer_id
        ).first()
        if not existing_customer:
            return EmailErrorResponse(message="CUSTOMER_NOT_FOUND")

        existing_request = db.query(Request).filter(
            Request.fromLocation == create_data.fromLocation.strip(),
            Request.toLocation == create_data.toLocation.strip(),
            Request.pickUpDate == create_data.pickUpDate,
            Request.pickUpTime == create_data.pickUpTime,
            Request.noOfAdults == create_data.noOfAdults,
            Request.noOfKids == create_data.noOfKids,
            Request.carType == (create_data.carType.strip() if create_data.carType else None),
            Request.requestStatus == STATUS_BID_OPEN,
        ).first()

        if existing_request:
            return EmailErrorResponse(message="REQUEST_ALREADY_PRESENT")

        new_request = Request(
            WIZZPNR=wizzpnr,
            fromLocation=create_data.fromLocation.strip(),
            fromLandmark=create_data.fromLandmark.strip() if create_data.fromLandmark else None,
            toLocation=create_data.toLocation.strip(),
            toLandmark=create_data.toLandmark.strip() if create_data.toLandmark else None,
            pickUpDate=create_data.pickUpDate,
            pickUpTime=create_data.pickUpTime,
            noOfAdults=create_data.noOfAdults,
            noOfKids=create_data.noOfKids,
            carType=create_data.carType.strip() if create_data.carType else None,
            acRequest=ac_request,
            carrierRequest=carrier_request,
            specialRequest=create_data.specialRequest.strip() if create_data.specialRequest else None,
            bidEndTime=create_data.bidEndTime,
            requestStatus=request_status,
            customerAppId=persisted_customer_id,
            requestType=request_type,
            tableTimestamp=_now_ist_naive(),
        )

        db.add(new_request)
        db.flush()

        if not commit:
            # Reopen / nested txn path: caller owns commit; do not emit
            # request.created here (separate contract from request.reopened).
            return new_request

        # PR43: request.created outbox in the SAME transaction (both flags).
        # When emit=False or flags off, maybe_append is a no-op / skipped.
        if emit:
            maybe_append_domain_event(
                db,
                event_type=EVENT_REQUEST_CREATED,
                aggregate_id=str(new_request.RID),
                payload={"requestId": int(new_request.RID)},
            )

        db.commit()
        db.refresh(new_request)

        if notify and background_tasks is not None:
            vendor_ids = get_vendors_for_request(
                db,
                create_data.fromLocation,
                create_data.toLocation,
            )
            if vendor_ids:
                background_tasks.add_task(
                    notify_vendors_for_request,
                    vendor_ids,
                    create_data,
                )

        return EmailErrorResponse(message="INSERTED")

    except HTTPException:
        if commit:
            db.rollback()
            raise
        raise
    except SQLAlchemyError as e:
        if commit:
            db.rollback()
        print(f"[insert_request_row] ERROR_INSERT: {e}")
        return EmailErrorResponse(message="ERROR_INSERT")
    except Exception as e:
        if commit:
            db.rollback()
        print(f"[insert_request_row] ERROR_EXCEPTION: {e}")
        return EmailErrorResponse(message="ERROR_INSERT")
    finally:
        if close_session:
            db.close()


def create_request(
    db: Session,
    create_data: RequestCreate,
    background_tasks: BackgroundTasks,
    user_id: Optional[str] = None,
    notify: bool = True,
    emit: bool = True,
):
    """
    Create a new request in requestTable.

    When ``user_id`` is provided (JWT ``sub`` from POST /insertrequest),
    body ``customerAppId`` must match; persisted identity is always the JWT sub.
    """
    return insert_request_row(
        db,
        create_data,
        user_id=user_id,
        commit=True,
        close_session=True,
        notify=notify,
        background_tasks=background_tasks,
        emit=emit,
    )

# def assign_driver_to_request(db:Session, request_data : AssignDriverRequest):
#     try : 
#         with db.begin():
#             # CHECK IF REQUEST EXISTS OR NOT
#             request = db.query(Request).filter(Request.RID == request_data.RID).first()
#             if not request:
#                 return EmailErrorResponse(message="NOT FOUND")
#             user_app_id = request.customerAppId

#             #Get Driver Details            
#             driver_details = db.query(DriverDetail).filter(DriverDetail.DDID == request_data.DRIVERID).first()
#             driver_name = driver_details.driverName if driver_details else None
#             driver_number = driver_details.driverNumber if driver_details else None

#             #Update the request with driver assignment
#             request.driverAssignedID = request_data.DRIVERID
#             request.tableTimestamp = datetime.now()
            

#             #Fetch Customer FCM Token to notify
#             customer = db.query(User).filter(User.userAppId == user_app_id).first()
#             fcm_token = ""
#             if customer and customer.fcmToken:
#                 fcm_token = customer.fcmToken.strip()

#             #Send Notifciaton 
#             if fcm_token and fcm_token.lower() not in ["","null"]:
#                 if driver_name or driver_number: 
#                     who = driver_name or "your driver"
#                     num = f" ({driver_number})" if driver_number else ""
#                     body = f"{who}{num} has been assigned to your request #{request_data.RID}."
#                 else:
#                     body = body = f"A driver has been assigned to your request #{request_data.RID}."

#                 try:
#                     notification = send_notification(
#                         title="Driver Assigned",
#                         body=body,
#                         fcm_token=fcm_token,
#                         url="//mytrips",
#                         type="passengernotification",
#                         sound_file="alarm_notification"
#                     )
#                 except Exception as e:
#                     print(f"[FCM] Failed for {request.customerAppId}: {e}")
#             return EmailErrorResponse(message="UPDATED")     
#     except SQLAlchemyError as e:
#         print(str(e))
#         db.rollback()
#         return EmailErrorResponse(message="DB ERROR")
#     except Exception as e:
#         db.rollback()
#         return EmailErrorResponse(message="INSER ERROR IN FUNCTION")
#     finally:
#         db.close()

def assign_driver_to_request(
    db: Session,
    request_data: AssignDriverRequest,
    user_id: Optional[str] = None,
    background_tasks: Optional[BackgroundTasks] = None,
    actor_auth_subject: Optional[str] = None,
):
    """
    Vendor assigns (or replaces) a driver on a confirmed request (PR13).

    Body: RID + DRIVERID only. JWT sub must equal requestWonBy and own the driver.
    Status gate: REQUEST - CONFIRMED only. No pickup-time gate.
    Same-driver replay → UPDATED without timestamp/notify churn.
    Different-driver replacement updates assignment + notifies customer once.
    Notification after commit via BackgroundTasks (own SessionLocal).

    PR40: emit driver.assignment_changed only when driverAssignedID changes.
    """
    try:
        if user_id is None or not str(user_id).strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to assign a driver",
            )

        vendor_id = str(user_id).strip()
        rid = int(request_data.RID)
        driver_id = int(request_data.DRIVERID)

        if rid <= 0 or driver_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid assignment payload",
            )

        request_row = (
            db.query(Request)
            .filter(Request.RID == rid)
            .with_for_update()
            .first()
        )

        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        won_by = (
            str(request_row.requestWonBy).strip()
            if request_row.requestWonBy is not None
            else ""
        )
        if won_by != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to assign a driver for this request",
            )

        if request_row.requestStatus != STATUS_REQUEST_CONFIRMED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        driver_row = (
            db.query(DriverDetail)
            .filter(DriverDetail.DDID == driver_id)
            .first()
        )
        if not driver_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Driver not found",
            )

        driver_owner = (
            str(driver_row.userAppId).strip() if driver_row.userAppId else ""
        )
        if driver_owner != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to assign this driver",
            )

        existing_assigned = request_row.driverAssignedID
        if existing_assigned is not None and int(existing_assigned) == driver_id:
            # Same-driver idempotent replay: success, no timestamp churn, no notify, no event.
            db.commit()
            return EmailErrorResponse(message="UPDATED")

        customer_app_id = request_row.customerAppId
        now = _now_ist_naive()
        request_row.driverAssignedID = driver_id
        request_row.tableTimestamp = now

        maybe_append_domain_event(
            db,
            event_type=EVENT_DRIVER_ASSIGNMENT_CHANGED,
            aggregate_id=str(rid),
            payload={
                "requestId": int(rid),
                "driverId": int(driver_id),
            },
            actor_auth_subject=actor_auth_subject,
        )

        db.commit()

        if background_tasks is not None and customer_app_id:
            background_tasks.add_task(
                notify_driver_assigned_to_customer_background,
                str(customer_app_id).strip(),
                rid,
                driver_id,
            )

        return EmailErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[assign_driver_to_request] ERROR: {e}")
        return EmailErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[assign_driver_to_request] ERROR_EXCEPTION: {e}")
        return EmailErrorResponse(message="ERROR")
    finally:
        db.close()

# def get_all_cancelled_requests_for_vendor(db: Session, vendor_id : str):
#     try:
#         with db.begin():
#             cancelled_requests = db.query(Request,
#                                 User.fullName, 
#                                 User.city,
#                                 User.userAppId,
#                                 User.alternateNumber,
#                                 User.profilePicture,
#                                 CustomerReview.generalRating
#                                 ).join(
#                 BidDetail, BidDetail.rID == Request.RID
#                 ).join(User, User.userAppId == Request.customerAppId).outerjoin(CustomerReview, CustomerReview.RID == Request.RID).filter(
#                     (Request.requestStatus == "BOOKING - CANCELLED BY USER") &
#                     (BidDetail.bidderID == vendor_id)).all()
#             if not cancelled_requests:
#                 return EmailErrorResponse(message="NO_REQUESTS",error="Database Error")
            
#             return [RequestConfirmedForVendorResponse(
#                 REQUESTID=requests.RID,
#                 FROMLOCATION=requests.fromLocation,
#                 FROMLANDMARK=requests.fromLandmark,                
#                 TOLOCATION=requests.toLocation,
#                 TOLANDMARK=requests.toLandmark,
#                 PICKUPDATE=requests.pickUpDate,
#                 PICKUPTIME=requests.pickUpTime,
#                 NOOFADULTS=requests.noOfAdults,
#                 NOOFKIDS=requests.noOfKids,
#                 CARTYPE=requests.carType,
#                 ACREQUEST=requests.acRequest,
#                 CARRIERREQUES=requests.carrierRequest,
#                 BIDENDTIME=requests.bidEndTime,
#                 REQUESTSTATUS=requests.requestStatus,
#                 PAYMENTSTATUS=requests.paymentStatus,
#                 CUSTOMERAPPID=requests.customerAppId,
#                 REQUESTWONBY=requests.requestWonBy,
#                 USERFULLNAME=full_name,
#                 CITY=city,
#                 PHONENUMBER=user_app_id,
#                 ALTNUMBER=alternate_number,
#                 PROFILEPIC=profile_picture,
#                 BIDAMOUNT=requests.finalAmount,
#                 CUSTREVIEW_GENERALRATING=general_rating,
#                 CANCELLATIONREASON=requests.rejectionReason
#             ) for requests, full_name,city,user_app_id,alternate_number,profile_picture,general_rating in cancelled_requests
#             ]
#     except SQLAlchemyError as e:
#         db.rollback()
#         return EmailErrorResponse(message="ERROR",error=str(e))
#     finally:
#         db.close()




def get_all_cancelled_requests_for_vendor(
    db: Session,
    user_id: str,
    vendor_id: Optional[str] = None,
) -> List[VendorCancelledHistoryItem]:
    """
    Vendor cancelled trip history (PR21).

    JWT sub is authoritative via requestWonBy. Optional transitional vendorId
    must match JWT or returns 403. Past BOOKING - CANCELLED BY USER only,
    newest pickup first (RID desc tie-break). Empty → [].
    Losing bidders never receive rows. No approval/lock gate.
    """
    if not user_id or not str(user_id).strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    owner_id = str(user_id).strip()

    if vendor_id is not None and str(vendor_id).strip():
        if str(vendor_id).strip() != owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized",
            )

    try:
        cancelled_requests = (
            db.query(
                Request,
                User.fullName,
                User.profilePicture,
            )
            .join(User, User.userAppId == Request.customerAppId)
            .filter(
                Request.requestStatus == STATUS_BOOKING_CANCELLED_BY_USER,
                Request.requestWonBy == owner_id,
            )
            .all()
        )
    except SQLAlchemyError:
        logger.exception(
            "get_all_cancelled_requests_for_vendor query failed owner_hash=%s",
            hash(owner_id) % 100000,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="HISTORY_QUERY_FAILED",
        )

    now_ist = _now_ist_naive()
    items: List[tuple] = []
    for req, full_name, profile_picture in cancelled_requests:
        _assert_history_row_identity(req)
        if getattr(req, "requestWonBy", None) != owner_id:
            logger.error(
                "HISTORY_DATA_INVALID rid=%s reason=won_by_mismatch",
                getattr(req, "RID", None),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="HISTORY_DATA_INVALID",
            )
        pickup_dt = _history_pickup_datetime(req)
        if pickup_dt >= now_ist:
            continue

        rid = int(req.RID)
        items.append(
            (
                pickup_dt,
                rid,
                VendorCancelledHistoryItem(
                    requestId=rid,
                    requestStatus=str(req.requestStatus),
                    fromLocation=_history_required_location(
                        req.fromLocation, rid=rid, field="from_location"
                    ),
                    toLocation=_history_required_location(
                        req.toLocation, rid=rid, field="to_location"
                    ),
                    pickupDate=_history_pickup_date_value(req, pickup_dt),
                    pickupTime=_history_pickup_time_value(req, pickup_dt),
                    noOfAdults=int(req.noOfAdults or 0),
                    noOfKids=int(req.noOfKids or 0),
                    carType=req.carType or "",
                    acRequested=bool(req.acRequest),
                    carrierRequested=bool(req.carrierRequest),
                    finalAmount=_history_nullable_final_amount(req.finalAmount),
                    customerDisplayName=_history_required_customer_name(
                        full_name, rid=rid
                    ),
                    customerProfileImageUrl=_history_optional_text(
                        profile_picture
                    ),
                    cancellationReason=_history_cancellation_reason(
                        req.rejectionReason
                    ),
                ),
            )
        )

    items.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in items]


def get_all_requests_by_request_status(db: Session, customer_id : int, request_status : str):
    try:
        requests = db.query(Request).filter(
            Request.customerAppId == customer_id, 
            Request.requestStatus == request_status
        ).all()
        if not requests:
            return NoBidsResponse(message="NO REQUESTS FOUND")
        
        return [RequestConfirmedCommonResponse(
                REQUESTID=req.RID,
                FROMLOCATION=req.fromLocation,
                FROMLANDMARK=req.fromLandmark,
                TOLOCATION=req.toLocation,
                TOLANDMARK=req.toLandmark,
                PICKUPDATE=req.pickUpDate,
                PICKUPTIME=req.pickUpTime,
                NOOFADULTS=req.noOfAdults,
                NOOFKIDS=req.noOfKids,
                CARTYPE=req.carType,
                ACREQUEST=req.acRequest,
                CARRIERREQUEST=req.carrierRequest,
                SPECIALREQUEST=req.specialRequest,
                BIDENDTIME=req.bidEndTime,
                REQUESTSTATUS=req.requestStatus,
                PAYMENTSTATUS=req.paymentStatus,
                CUSTOMERAPPID=req.customerAppId,
                REQUESTWONBY=req.requestWonBy,
                NOOFBIDS=req.noOfBids,
                TABLETIMESTAMP=req.tableTimestamp
            ) for req in requests]
    except SQLAlchemyError:
        return NoBidsResponse(message="ERROR_PREPARE")
    finally:
        db.close()
