from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
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
                                     RequestConfirmedCommonResponse,GetBookingReportResponse)
from ..schemas.request_type_details import RequestTypeBase
from ..utils.common import ErrorResponse,EmailErrorResponse
from ..services.notifications import send_notification_to_all_vendors,send_notification
from datetime import datetime,timedelta
from fastapi import BackgroundTasks
from ..crud.bid import delete_bid_with_bid



def get_all_open_requests(db : Session):
    try:
        with db.begin():
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


def get_all_requests_for_user(db : Session, customer_app_id : str):    
    try:
        requests = db.query(
                Request,
                DriverDetail.driverName,
                DriverDetail.driverNumber,
                DriverDetail.driverPhoto,
                DriverDetail.driverDOB,
                DriverDetail.driverGender,
                DriverDetail.driverCity,
                DriverDetail.driverLicense,
                BidDetail.bidAmount,
                BidDetail.CARID,
                CarDetail.carRegNo,
                CarDetail.carModel,
                CarDetail.modelYear,
                CarDetail.carColor,
                CarDetail.ownerName,
                CarDetail.registrationDoc,
                CarDetail.powerOfAttorneyDoc,
                CarDetail.registeredOn,
                CarDetail.carOwnedBySameVendor,
                CarDetail.CTD,
                CarTypeDetail.car_type
            ).outerjoin(
            DriverDetail, DriverDetail.DDID == Request.driverAssignedID
            ).outerjoin(
                BidDetail, BidDetail.rID == Request.RID
            ).outerjoin(
                CarDetail, CarDetail.CARID == BidDetail.CARID
            ).outerjoin(
                CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
            ).filter(Request.customerAppId == customer_app_id).order_by(Request.tableTimestamp.desc()).all()
        if not requests:
            return NoBidsResponse(message="NO REQUESTS FOUND")
        return [
                RequestForUserResponse(
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
                    FINALAMOUNT=req.finalAmount,
                    NOOFBIDS=req.noOfBids,
                    REJECTIONREASON=req.rejectionReason,
                    REOPENBOOKING=req.requestReopened,
                    TABLETIMESTAMP=req.tableTimestamp,
                    REVIEWDONE=req.reviewDone,
                    DRIVERNAME=driver_name,
                    DRIVERNUMBER=driver_number,
                    DRIVERPHOTO=driver_photo,
                    DRIVERDOB=driver_dob,
                    DRIVERGENDER=driver_gender,
                    DRIVERCITY=driver_city,
                    DRIVERLICENSE=driver_license,
                    BIDAMOUNT=bid_amount,
                    CARID=car_id,
                    CARREGNO=car_reg_no,
                    CARMODEL=car_model,
                    MODELYEAR=model_year,
                    CARCOLOR=car_color,
                    OWNERNAME=owner_name,
                    REGISTRATIONDOC=registration_doc,
                    POWEROFATTORNEYDOC=power_of_attorney_doc,
                    REGISTEREDON=registered_on,
                    CAROWNEDBYSAMEVENDOR=car_owned_by_same_vendor,
                    CTD=ctd,
                    CAR_TYP=car_type

                )
                    for req,driver_name,driver_number,driver_photo,driver_dob,
                    driver_gender,driver_city,driver_license,bid_amount,car_id,car_reg_no,
                     car_model,model_year,car_color,owner_name,registration_doc,
                      power_of_attorney_doc,registered_on,car_owned_by_same_vendor,
                       ctd,car_type in requests
            ]
    except SQLAlchemyError:
        return NoBidsResponse(message="ERROR_PREPARE")
    finally:
        db.close()

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
                TABLETIMESTAMP=req.tableTimestamp,
                WIZZPNR=req.WIZZPNR,
                FINALAMOUNT=req.finalAmount,
                REJECTIONREASON=req.rejectionReason,
                REQUESTOPENED=req.requestReopened,
                REVIEWDONE=req.reviewDone                
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
        if not types:
            return ErrorResponse(message="NO_REQUEST_TYPES_FOUND")
        return [RequestTypeBase.from_orm(type) for type in types]
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    

def delete_request(db: Session, r_id : int):
    try:
        updated = db.query(Request).filter(Request.RID == r_id).update(
            {Request.requestStatus: "REQUEST - CANCELLED BY USER"}
        )
        db.commit()

        if updated==0:
            return ErrorResponse(message="NO ROWS DELETED")
        
        return ErrorResponse(message="DELETED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="DELETED ERROR IN FUNCTION")
    finally:
        db.close()
    
def update_request(db : Session, request_data : RequestUpdate):
    try:
        request = db.query(Request.noOfBids).filter(Request.RID == request_data.RID).first()

        if not request: 
            return ErrorResponse(message="ERROR")
        
        no_of_bids = request.noOfBids

        if no_of_bids > 0:
            return ErrorResponse(message="NO OF BIDS MORE THAN 0")
        
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
            Request.carrierRequest : 1 if request_data.carrierRequest else 0,
            Request.specialRequest : request_data.specialRequest,
            Request.bidEndTime : request_data.bidEndTime,
            Request.tableTimestamp : func.current_timestamp()
        })
        db.commit()

        if updated==0:
            return ErrorResponse(message="FAILED")
        
        return ErrorResponse(message="SUCCESS")
        
    except SQLAlchemyError as e:
        db.rollback()
        print(str(e))
        return EmailErrorResponse(message="ERROR", error=str(e))    
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_EXCEPTION", error=str(e))    
    finally:
        db.close()
    
def accept_by_vendor(db: Session, rid : int, vendor_id : int, final_amount : float):
    try:
        requestupdate = db.query(Request).filter(Request.RID == rid).update({
            Request.requestStatus: "REQUEST - CONFIRMED",
            Request.requestWonBy: vendor_id,
            Request.finalAmount:final_amount,
            Request.tableTimestamp:func.current_timestamp()

        })
        db.commit()

        if requestupdate==0:
            return ErrorResponse(message="REQUEST TABLE UPDATE FAILED")
        
        bidupdate = db.query(BidDetail).filter((BidDetail.rID == rid)&(BidDetail.bidderID == vendor_id)).update({
            BidDetail.bidStatus: "BID - CONFIRMED",
            BidDetail.tableTimestamp: func.current_timestamp()
        })
        db.commit()

        if bidupdate==0:
            return ErrorResponse(message="BID UPDATE STATUS FAILED")
        
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR") 
    finally:
        db.close()

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

def reject_request_by_vendor(
    db: Session,
    rid: int,
    bid_id: int,
    rejection_reason: str
) -> EmailErrorResponse | ErrorResponse:
    try:
        # One atomic transaction: if any step fails, nothing is saved
        with db.begin():
            # 1) Delete the bid (must match both BID and rID)
            deleted = (
                db.query(BidDetail)
                  .filter(BidDetail.BID == bid_id, BidDetail.rID == rid)
                  .delete(synchronize_session=False)
            )
            if deleted == 0:
                # Raising inside `with db.begin()` will auto-rollback
                raise ValueError("NO ROWS DELETED")

            # 2) Recompute accurate noOfBids for this request (safer than decrement)
            new_count = (
                db.query(func.count(BidDetail.BID))
                  .filter(BidDetail.rID == rid)
                  .scalar()
            )

            # 3) Update the request row only if delete succeeded
            updated = (
                db.query(Request)
                  .filter(Request.RID == rid)
                  .update(
                      {
                          Request.noOfBids: new_count,
                          Request.requestStatus: "BID - OPEN",
                          Request.rejectionReason: rejection_reason,
                          Request.tableTimestamp: func.current_timestamp(),
                      },
                      synchronize_session=False,
                  )
            )
            if updated == 0:
                raise ValueError("REQUEST TABLE UPDATE FAILED")

        # If we reached here, the transaction committed successfully
        return EmailErrorResponse(message="UPDATED")

    except ValueError as ve:
        # Transaction already rolled back by context manager
        return ErrorResponse(message=str(ve))

    except SQLAlchemyError as e:
        # Any DB error → rollback (context manager handles it), return error
        return ErrorResponse(message="ERROR")
    
def cancel_handshake(db:Session, rid :int):
    try : 
        request_update = db.query(Request).filter(Request.RID == rid).update({
            Request.requestStatus: "BID - OPEN",
            Request.tableTimestamp: func.current_timestamp()
        })
        db.commit()

        if request_update == 0:
            return ErrorResponse(message="REQUEST TABLE UPDATE FAILED")
        
        bid_update = db.query(BidDetail).filter(BidDetail.rID == rid).update({
            BidDetail.bidStatus:"BID - OPEN",
            BidDetail.tableTimestamp:func.current_timestamp()
        })
        db.commit()
        if bid_update == 0:
            return ErrorResponse(message="BID TABLE UPDATE FAILED")
        return ErrorResponse(message="CANCELLED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    

def booking_cancelled_by_user(db:Session, rid : int, rejection_reason : str):
    try:
        update = db.query(Request).filter(Request.RID == rid).update({
            Request.requestStatus:"BOOKING - CANCELLED BY USER'",
            Request.rejectionReason:rejection_reason,
            Request.tableTimestamp:func.current_timestamp()
        })
        db.commit()
        if update==0:
            return ErrorResponse(message="REQUEST TABLE UPDATE FAILED")        
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
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
    

def get_all_confirmed_requests_for_vendor(db: Session, vendor_id : str):
    try:
        with db.begin():
            confirmed_requests = db.query(Request,
                                User.fullName, 
                                User.city,
                                User.userAppId,
                                User.alternateNumber,
                                User.profilePicture,
                                CustomerReview.generalRating
                                ).join(
                BidDetail, BidDetail.rID == Request.RID
                ).join(User, User.userAppId == Request.customerAppId).outerjoin(CustomerReview, CustomerReview.RID == Request.RID).filter(
                    (Request.requestStatus == "REQUEST - CONFIRMED") &
                    (BidDetail.bidderID == vendor_id)).all()
            if not confirmed_requests:
                return EmailErrorResponse(message="NO_REQUESTS",error="Database Error")
            
            return [RequestConfirmedForVendorResponse(
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
                USERFULLNAME=full_name,
                CITY=city,
                PHONENUMBER=user_app_id,
                ALTNUMBER=alternate_number,
                PROFILEPIC=profile_picture,
                BIDAMOUNT=requests.finalAmount,
                CUSTREVIEW_GENERALRATING=general_rating,
                CANCELLATIONREASON=requests.rejectionReason
            ) for requests, full_name,city,user_app_id,alternate_number,profile_picture,general_rating in confirmed_requests
            ]
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR",error=str(e))
    finally:
        db.close()
    

def reopen_request(db : Session, r_id : int, background_tasks : BackgroundTasks):
    """
    Reopen a booking by setting requestReopened=1 and creating a new request.
    """
    try:
        with db.begin():
            update = db.query(Request).filter(Request.RID == r_id).update({
                Request.requestReopened : True,
                Request.tableTimestamp : datetime.now()
            })
            db.flush()
            if update ==0 : 
                return EmailErrorResponse(message="REQUEST_NOT_FOUND")
            
            request = db.query(Request.fromLocation,
                               Request.fromLandmark,
                               Request.toLocation,
                               Request.toLandmark,
                               Request.pickUpDate,
                               Request.pickUpTime,
                               Request.noOfAdults,
                               Request.noOfKids,
                               Request.carType,
                               Request.acRequest,
                               Request.carrierRequest,
                               Request.bidEndTime,
                               Request.customerAppId).filter(Request.RID == r_id).first()
            if not request:
                db.rollback()
                return EmailErrorResponse(message="REQUEST_NOT_FOUND")

            (
                from_location,
                from_landmark,
                to_location,
                to_landmark,
                pick_up_date,
                pick_up_time,
                no_of_adults,
                no_of_kids,
                car_type,
                ac_request,
                carrier_request,
                bid_end_time,
                customer_app_id
            ) = request

            #Modify request 

            pickup_datetime = datetime.combine(pick_up_date,pick_up_time)
            modified_pick_up_time = (pickup_datetime + timedelta(minutes=5)).time()
            print(modified_pick_up_time)

            create_data = RequestCreate(
                fromLocation=from_location,
                fromLandmark=from_landmark,
                toLocation=to_location,
                toLandmark=to_landmark,
                pickUpDate=pick_up_date,
                pickUpTime=modified_pick_up_time,
                noOfAdults=no_of_adults,
                noOfKids=no_of_kids,
                carType=car_type,
                acRequest=ac_request,
                carrierRequest=carrier_request,
                bidEndTime=bid_end_time,
                customerAppId=customer_app_id
            )

            create_result = create_request(db,create_data,background_tasks=background_tasks,
                                           emit=True,notify=True)
            if create_result.message != "INSERTED":
                db.rollback()
                return EmailErrorResponse(message=create_result.message, error=create_result.error)
            db.commit()
            return EmailErrorResponse(message="UPDATED")                        
    except SQLAlchemyError as e :
        db.rollback()
        return EmailErrorResponse(message="ERROR",error=str(e))
    
    except ValueError as e : 
        db.rollback()
        return EmailErrorResponse(message="ERROR_INVALID_FORMAT",error=str(e))
    finally:
        db.close()
    


def create_request(
    db: Session,
    create_data: RequestCreate,
    background_tasks: BackgroundTasks,
    notify: bool = True,
    emit: bool = True,
):
    """
    Create a new request in requestTable.
    """
    try:
        # 1) Validate customer exists
        existing_customer = db.query(User).filter(
            User.userAppId == create_data.customerAppId
        ).first()
        if not existing_customer:
            return EmailErrorResponse(message="CUSTOMER_NOT_FOUND")

        # 2) Check duplicate
        existing_request = db.query(Request).filter(
            Request.fromLocation == create_data.fromLocation.strip(),
            Request.toLocation == create_data.toLocation.strip(),
            Request.pickUpDate == create_data.pickUpDate,
            Request.pickUpTime == create_data.pickUpTime,
            Request.noOfAdults == create_data.noOfAdults,
            Request.noOfKids == create_data.noOfKids,
            Request.carType == (create_data.carType.strip() if create_data.carType else None),
            Request.requestStatus == "BID - OPEN",
        ).first()

        if existing_request:
            return EmailErrorResponse(message="REQUEST_ALREADY_PRESENT")

        # 3) Build new request row
        new_request = Request(
            WIZZPNR=create_data.wizzpnr.strip() if create_data.wizzpnr else None,
            fromLocation=create_data.fromLocation.strip(),
            fromLandmark=create_data.fromLandmark.strip() if create_data.fromLandmark else None,
            toLocation=create_data.toLocation.strip(),
            toLandmark=create_data.toLandmark.strip() if create_data.toLandmark else None,
            pickUpDate=create_data.pickUpDate,
            pickUpTime=create_data.pickUpTime,
            noOfAdults=create_data.noOfAdults,
            noOfKids=create_data.noOfKids,
            carType=create_data.carType.strip() if create_data.carType else None,
            acRequest=create_data.acRequest,
            carrierRequest=create_data.carrierRequest,
            specialRequest=create_data.specialRequest.strip() if create_data.specialRequest else None,
            bidEndTime=create_data.bidEndTime,
            requestStatus="BID - OPEN",
            customerAppId=create_data.customerAppId,
            requestType=create_data.requestType if create_data.requestType else 1,
            tableTimestamp=datetime.now()
        )

        db.add(new_request)
        db.commit()
        db.refresh(new_request)

        # 4) Background notification (optional)
        if notify:
            background_tasks.add_task(
                send_notification_to_all_vendors,
                "🚖 New Cab Request Alert! 🚖",
                f"A customer has just created a new cab request from {create_data.fromLocation} to {create_data.toLocation}! 🏁💨 Submit your bid now and secure the ride.",
                "passenger_notification",
                "alarm_notification",
            )

        # If your model has an RID (auto or generated), include it in the response
        return EmailErrorResponse(message="INSERTED", RID=getattr(new_request, "RID", None))

    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_INSERT", error=str(e))
    finally:
        db.close()

def assign_driver_to_request(db:Session, request_data : AssignDriverRequest):
    try : 
        with db.begin():
            # CHECK IF REQUEST EXISTS OR NOT
            request = db.query(Request).filter(Request.RID == request_data.RID).first()
            if not request:
                return EmailErrorResponse(message="NOT FOUND")
            user_app_id = request.customerAppId

            #Get Driver Details            
            driver_details = db.query(DriverDetail).filter(DriverDetail.DDID == request_data.DRIVERID).first()
            driver_name = driver_details.driverName if driver_details else None
            driver_number = driver_details.driverNumber if driver_details else None

            #Update the request with driver assignment
            request.driverAssignedID = request_data.DRIVERID
            request.tableTimestamp = datetime.now()
            

            #Fetch Customer FCM Token to notify
            customer = db.query(User).filter(User.userAppId == user_app_id).first()
            fcm_token = ""
            if customer and customer.fcmToken:
                fcm_token = customer.fcmToken.strip()

            #Send Notifciaton 
            if fcm_token and fcm_token.lower() not in ["","null"]:
                if driver_name or driver_number: 
                    who = driver_name or "your driver"
                    num = f" ({driver_number})" if driver_number else ""
                    body = f"{who}{num} has been assigned to your request #{request_data.RID}."
                else:
                    body = body = f"A driver has been assigned to your request #{request_data.RID}."

                try:
                    notification = send_notification(
                        title="Driver Assigned",
                        body=body,
                        fcm_token=fcm_token,
                        url="//mytrips",
                        type="passengernotification",
                        sound_file="alarm_notification"
                    )
                except Exception as e:
                    print(f"[FCM] Failed for {request.customerAppId}: {e}")
            return EmailErrorResponse(message="UPDATED")     
    except SQLAlchemyError as e:
        print(str(e))
        db.rollback()
        return EmailErrorResponse(message="DB ERROR")
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="INSER ERROR IN FUNCTION")
    finally:
        db.close()
    

def get_all_cancelled_requests_for_vendor(db: Session, vendor_id : str):
    try:
        with db.begin():
            cancelled_requests = db.query(Request,
                                User.fullName, 
                                User.city,
                                User.userAppId,
                                User.alternateNumber,
                                User.profilePicture,
                                CustomerReview.generalRating
                                ).join(
                BidDetail, BidDetail.rID == Request.RID
                ).join(User, User.userAppId == Request.customerAppId).outerjoin(CustomerReview, CustomerReview.RID == Request.RID).filter(
                    (Request.requestStatus == "BOOKING - CANCELLED BY USER") &
                    (BidDetail.bidderID == vendor_id)).all()
            if not cancelled_requests:
                return EmailErrorResponse(message="NO_REQUESTS",error="Database Error")
            
            return [RequestConfirmedForVendorResponse(
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
                USERFULLNAME=full_name,
                CITY=city,
                PHONENUMBER=user_app_id,
                ALTNUMBER=alternate_number,
                PROFILEPIC=profile_picture,
                BIDAMOUNT=requests.finalAmount,
                CUSTREVIEW_GENERALRATING=general_rating,
                CANCELLATIONREASON=requests.rejectionReason
            ) for requests, full_name,city,user_app_id,alternate_number,profile_picture,general_rating in cancelled_requests
            ]
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR",error=str(e))
    finally:
        db.close()


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
