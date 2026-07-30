from sqlalchemy import func,cast, Float, Integer
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..models.vendor_reviews import VendorReview
from ..models.customer_reviews import CustomerReview
from ..models.user_table import User
from ..models.request_table import Request
from ..models.bid_details import BidDetail
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..models.driver_details import DriverDetail
from ..models.request_table import Request
from ..schemas.vendor_reviews import ReviewDetail,NoReviewResponse,ReviewCreate
from ..schemas.customer_reviews import CustomerReviewDetail,NoReviewResponse,CreateCustomerReview
from ..utils.common import ErrorResponse,EmailErrorResponse
from datetime import datetime
           

def get_reviews_for_vendor(db: Session, vendor_id: str):
    try:
        reviews = db.query(
            VendorReview,
            User.fullName,
            User.profilePicture,

            # request
            Request.fromLocation,
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
            Request.specialRequest,

            # bid
            BidDetail.bidAmount,
            BidDetail.CARID,

            # car
            CarDetail.carRegNo,
            CarDetail.carModel,
            CarDetail.modelYear,
            CarDetail.carColor,
            CarDetail.ownerName,

            # car type
            CarTypeDetail.car_type,

            # driver
            DriverDetail.driverName

        ).join(
            User, VendorReview.customerAppId == User.userAppId
        ).join(
            Request, Request.RID == VendorReview.RID
        ).outerjoin(
            BidDetail,
            (BidDetail.rID == VendorReview.RID) &
            (BidDetail.bidderID == VendorReview.VENDORID)
        ).outerjoin(
            CarDetail, CarDetail.CARID == BidDetail.CARID
        ).outerjoin(
            CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
        ).outerjoin(
            DriverDetail, DriverDetail.DDID == Request.driverAssignedID
        ).filter(
            VendorReview.VENDORID == vendor_id
        ).order_by(
            VendorReview.VRID.desc()
        ).all()

        if not reviews:
            return NoReviewResponse(message="NO REVIEWS FOUND")

        return [
            ReviewDetail(
                CUSTOMERID=review.customerAppId,
                CUSTOMERNAME=full_name,
                REQUESTID=review.RID,
                VENDORID=review.VENDORID,

                DRIVERBEHAVIOUR=review.driverBehaviour,
                PUNCTUALITY=review.punctuality,
                CARCONDITION=review.carCondition,
                CLEANLINESS=review.cleanliness,
                REFRESHMENTS=review.refreshments,
                COMMENTS=review.comments,

                CUSTOMER_PROFILEPIC=profile_picture,

                # request
                REQ_FROMLOCATION=from_location,
                REQ_FROMLANDMARK=from_landmark,
                REQ_TOLOCATION=to_location,
                REQ_TOLANDMARK=to_landmark,
                REQ_PICKUPDATE=pickup_date,
                REQ_PICKUPTIME=pickup_time,
                REQ_NOOFADULTS=no_of_adults,
                REQ_NOOFKIDS=no_of_kids,
                REQ_CARTYPE=req_car_type,
                REQ_ACREQUEST=ac_request,
                REQ_CARRIERREQUEST=carrier_request,
                REQ_SPECIALREQUEST=special_request,

                # bid
                BID_BIDAMOUNT=bid_amount,
                BID_CARID=car_id,

                # car
                CAR_REGNO=car_reg_no,
                CAR_MODEL=car_model,
                CAR_MODELYEAR=model_year,
                CAR_COLOR=car_color,
                CAR_OWNERNAME=owner_name,

                # car type
                CARTYPE=car_type_label,

                # driver
                DRIVER_NAME=driver_name
            )
            for (
                review,
                full_name,
                profile_picture,

                from_location,
                from_landmark,
                to_location,
                to_landmark,
                pickup_date,
                pickup_time,
                no_of_adults,
                no_of_kids,
                req_car_type,
                ac_request,
                carrier_request,
                special_request,

                bid_amount,
                car_id,

                car_reg_no,
                car_model,
                model_year,
                car_color,
                owner_name,

                car_type_label,
                driver_name
            ) in reviews
        ]

    except SQLAlchemyError as e:
        db.rollback()
        return NoReviewResponse(message="ERROR_PREPARE", error=str(e))

def get_reviews_for_customer(db : Session, customer_id : str):
    try:
        customer_reviews = db.query(
            CustomerReview, 
            User.fullName,User.profilePicture,
            Request.fromLocation,
            Request.fromLandmark,
            Request.toLocation,
            Request.toLandmark,
            Request.pickUpDate,
            Request.pickUpTime
        ).join(
            User, CustomerReview.ratingReceiverUserAppId == User.userAppId
        ).outerjoin(
            Request, Request.RID == CustomerReview.RID
        ).order_by(CustomerReview.RID.desc()).filter(
            CustomerReview.ratingReceiverUserAppId == customer_id
        ).all()

        if not customer_reviews:
            return NoReviewResponse(message="NO REVIEWS FOUND")
        
        return [
            CustomerReviewDetail(
                RID=req.RID,
                ratingGivenBy=req.ratingGiverUserAppId,
                ratingReceivedBy=req.ratingReceiverUserAppId,
                generalRating=req.generalRating,
                comments=req.comments,
                vendorFullName=full_name,
                vendorProfilePicture=profile_picture,
                fromLocation=from_location,
                fromLandmark=from_landmark,
                toLocation=to_location,
                toLandmark=to_landmark,
                pickUpDate=pickup_date,
                pickUpTime=pickup_time
            ) for req, full_name,profile_picture,from_location,from_landmark,
            to_location,to_landmark,pickup_date,pickup_time in customer_reviews
        ]
    except SQLAlchemyError:
        return NoReviewResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    
    

def create_vendor_review(db: Session, feedback_data : ReviewCreate):
    try:        
            existing_feedback = db.query(VendorReview).filter(
                (VendorReview.customerAppId == feedback_data.customerAppId) &
                (VendorReview.RID == feedback_data.RID)
                ).first()
            
            if existing_feedback:
                return ErrorResponse(message="FEEDBACK ALREADY PRESENT")

            # Calculate cumulative rating
            cumulative_rating = (
                feedback_data.driverBehaviour + 
                feedback_data.punctuality +
                feedback_data.carCondition +
                feedback_data.cleanliness
            ) / 4

            # Fetch vendor's current rating and review count

            vendor = db.query(
                cast(User.rating, Float),
                cast(User.totalNoOfReviews, Integer)
            ).filter(
                User.userAppId == feedback_data.VENDORID
            ).first()

            if not vendor:
                return ErrorResponse(message="ERROR")
                        
            rating, total_no_of_reviews = vendor or (0.0, 0)
            
            if total_no_of_reviews == 0:
                final_rating = cumulative_rating
                final_review_count = 1
            else:
                final_review_count = total_no_of_reviews + 1
                final_rating = ((rating * total_no_of_reviews) + cumulative_rating) / final_review_count

            new_feedback = VendorReview(
                customerAppId=feedback_data.customerAppId,
                RID=feedback_data.RID,
                VENDORID=feedback_data.VENDORID,
                driverBehaviour=feedback_data.driverBehaviour,
                punctuality=feedback_data.punctuality,
                carCondition=feedback_data.punctuality,
                cleanliness=feedback_data.cleanliness,
                refreshments=1 if feedback_data.refreshments else 0,
                comments=feedback_data.comments,
                tableTimestamp=func.current_timestamp()
            )

            db.add(new_feedback)
            
            # Update userTable with new rating and review count
            update_user = db.query(User).filter(User.userAppId == feedback_data.VENDORID).update({
                User.rating:round(final_rating,2),
                User.totalNoOfReviews:final_review_count
            })
            
            if update_user==0:
                return ErrorResponse(message="ERROR")
            
            # Update requestTable to mark reviewDone
            review_done = 'Y'
            update_request = db.query(Request).filter(Request.RID == feedback_data.RID).update({
                Request.reviewDone:review_done  
            })            
            if update_request==0:
                return ErrorResponse(message="ERROR")            
            db.commit()
            return ErrorResponse(message="INSERTED")

    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()

def insert_customer_review(db: Session, create_data : CreateCustomerReview):
    try:
        with db.begin():
            # --- 1. Duplicate check ---
            exists = db.query(CustomerReview).filter(
                CustomerReview.RID == create_data.RID,
                CustomerReview.ratingGiverUserAppId == create_data.VENDORID
                ).first()
            if exists:
                return EmailErrorResponse(message="FEEDBACK_ALREADY_PRESENT")
            
            # --- 2. Fetch receiver (customer) ---
            receiver = db.query(User).filter(User.userAppId == create_data.CUSTOMERID).first()
            if not receiver:
                return EmailErrorResponse(message="RECEIVER_NOT_FOUND")
            
            prev_rating = float(receiver.customerRating or 0)
            prev_count = int(receiver.totalCustomerReviews or 0)

            new_count = prev_count + 1
            new_avg = create_data.RATING if prev_count == 0 else ((prev_rating * prev_count) + create_data.RATING) / new_count
            rounded_avg = round(new_avg,2)

            new_review = CustomerReview(
                RID=create_data.RID,
                ratingGiverUserAppId=create_data.VENDORID,
                ratingReceiverUserAppId=create_data.CUSTOMERID,
                generalRating = create_data.RATING,
                comments=create_data.COMMENTS,
                tableTimestamp=datetime.now()
            )
            
            db.add(new_review)

            # Update aggregates
            receiver.customerRating = new_avg
            receiver.totalCustomerReviews = new_count

            request = db.query(Request).filter(Request.RID == create_data.RID).first()
            if request:
                request.customerReviewDone = 'Y'
            
            db.commit()

            return EmailErrorResponse(message="INSERTED")
    except SQLAlchemyError as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR_DB",error=str(e))
    except Exception as e:
        db.rollback()
        return EmailErrorResponse(message="ERROR",error=str(e))

