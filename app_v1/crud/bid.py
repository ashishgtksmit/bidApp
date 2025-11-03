from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..models.bid_details import BidDetail
from ..models.user_table import User
from ..models.tags_table import Tag
from ..models.request_table import Request
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..schemas.bid_details import BidDetail as BidDetailSchema, NoBidResponse, BidInsert
from ..utils.common import ErrorResponse,EmailErrorResponse
from datetime import datetime
from ..utils.common import parse_dob



def get_bids_for_request(db : Session, rid : int):    
    try:
        bids = db.query(BidDetail, 
                        
                        User.fullName, User.rating, User.totalNoOfReviews, User.fcmToken, User.profilePicture,
                        User.dob, User.joiningDate, User.city, User.tags, User.noOfTripsCompleted,
                        
                        CarDetail.CARID,
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
                        CarDetail.userAppId,

                        CarTypeDetail.car_type,
                        CarTypeDetail.car_sub_type,
                        CarTypeDetail.capacity,
                        CarTypeDetail.image_url                        
                        ).join(
                            User, User.userAppId == BidDetail.bidderID
                        ).outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID).outerjoin(
                            CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
                        ).filter(
                            BidDetail.rID == rid
                        ).all()
        if not bids:
            return ErrorResponse(message="NO BIDS FOUND")
        
        result = []

        for (bid,fullName,rating,totalNoOfReviews,fcmToken,profilePicture,
             dob,joiningDate,city,tags_str,noOfTripsCompleted,
             car_id,car_reg_no,car_model,model_year,car_color,owner_name,registration_doc,
             power_of_attorney_doc,registered_on,admin_approved,car_owned_by_same_vendor,ctd,
             image_vehicle_front,image_vehicle_side,user_app_id,car_type,car_sub_type,capacity,image_url                          
             ) in bids:
            
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


            safe_rating = rating if rating is not None else 0.0    
                    

            result.append(BidDetailSchema(
                BIDID=bid.BID,
                BIDDERID=bid.bidderID,
                BIDAMOUNT=bid.bidAmount,
                BIDDONEON=bid.tableTimestamp.strftime('%Y-%m-%d %H:%M:%S') if bid.tableTimestamp else None,
                BIDDERNAME=fullName,
                BIDDERRATING=safe_rating,
                TOTALNOOFREVIEWS=totalNoOfReviews,
                FCMTOKEN=fcmToken,
                PROFILEPIC=profilePicture,
                BIDSTATUS=bid.bidStatus,
                DOB=parse_dob(dob),
                JOININGDATE=joiningDate,
                BASELOCATION=city,
                TAGS=tag_names,
                NOOFTRIPSCOMPLETED=noOfTripsCompleted,
                CARID=car_id,
                CARREGNO=car_reg_no,
                CARMODEL=car_model,
                MODELYEAR=model_year,
                CARCOLOR=car_color,
                OWNERNAME=owner_name,
                REGISTRATIONDOC=registration_doc,
                POWEROFATTORNEYDOC=power_of_attorney_doc,
                REGISTEREDON=registered_on.strftime('%Y-%m-%d %H:%M:%S') if registered_on else None,
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
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()
    

def delete_bid_with_bid(db : Session, rid : int, bid : int):
    try:
        deleted = db.query(BidDetail).filter(BidDetail.BID == bid).delete()        
        if deleted == 0:
            return ErrorResponse(message="NO ROWS DELETED")
        
        updated = db.query(Request).filter(Request.RID == rid).update(
            {
                Request.noOfBids: Request.noOfBids - 1,
                Request.tableTimestamp: func.current_timestamp()
            }
        )        
        if updated ==0:
            return ErrorResponse(message="NO ROWS UPDATED")
        db.commit()
        return ErrorResponse(message="DELETED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="DELETED ERROR IN FUNCTION")
    finally:
        db.close()
    
def update_bid(db:Session, bid : int, bidamount : float):
    try : 
        bidupdate = db.query(BidDetail).filter(BidDetail.BID == bid).update({
            BidDetail.bidAmount: bidamount,
            BidDetail.tableTimestamp : func.current_timestamp()
        })
        
        if bidupdate == 0:
            return ErrorResponse(message="BID UPDATE ERROR")        
        db.commit()
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
def accept_bid(db:Session, rid :int, vendor_id :int):
    try : 
        requestupdate = db.query(Request).filter(Request.RID == rid).update({
            Request.requestStatus:"BID - CONFIRMED",
            Request.tableTimestamp:func.current_timestamp()
        })        
        if requestupdate==0:
            return ErrorResponse(message="INSER ERROR IN FUNCTION")
        
        bidupdate = db.query(BidDetail).filter((BidDetail.rID == rid)&(BidDetail.bidderID == vendor_id)).update({
            BidDetail.bidStatus:"BID - CONFIRMED",
            BidDetail.tableTimestamp:func.current_timestamp()
        })
        
        if bidupdate==0:
            return ErrorResponse(message="NOT UPDATED")
        db.commit()
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
def insert_bid(db: Session, bid_data : BidInsert):    
    try :         
        existing_bid = db.query(BidDetail).filter((BidDetail.rID == bid_data.RID) &
                                                (BidDetail.bidderID == bid_data.bidderID) &
                                                (BidDetail.CARID == bid_data.assignedVehicleID)).first()
        if existing_bid:
            return ErrorResponse(message="BID ALREADY PRESENT")
        
        # Insert new bid
        new_bid = BidDetail(
            rID = bid_data.RID,
            bidderID = bid_data.bidderID,
            CARID = bid_data.assignedVehicleID,
            bidAmount = bid_data.bidAmount,
            bidStatus = "BID - OPEN",
            tableTimestamp = datetime.utcnow()
        )
        db.add(new_bid)
        
        # Update requestTable 
        updated = db.query(Request).filter(Request.RID == bid_data.RID).update({
            Request.noOfBids: Request.noOfBids + 1,
            Request.tableTimestamp: func.current_timestamp()
        })
        
        if updated ==0 :
            raise RuntimeError("UPDATE REQUEST TABLE FAILED")
        
        db.commit()
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message=f"ERROR: {e.__class__.__name__}")
    finally:
        db.close()








