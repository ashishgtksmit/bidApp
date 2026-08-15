from sqlalchemy import String as SAString, BigInteger, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from fastapi import BackgroundTasks, HTTPException, status
from typing import Optional
from ..models.bid_details import BidDetail
from ..models.user_table import User
from ..models.tags_table import Tag
from ..models.request_table import Request
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..schemas.bid_details import (
    CustomerBidDetail,
    BidInsert,
    UpdateCarIdForBidRequest,
)
from ..utils.common import ErrorResponse,EmailErrorResponse
from datetime import date, datetime
from ..utils.common import parse_dob
from ..services.vendor_filtering import get_vendors_who_bid_on_request
from ..services.notifications import (
    send_notification_to_selected_users,
    send_notification_to_user,
    notify_vendor_bid_accepted,
    FCMSendDrivers,
    FCMSend,
)
from zoneinfo import ZoneInfo

from ..events.outbox import maybe_append_domain_event
from ..events.registry import EVENT_BID_ACCEPTED


# Customer GET /getallbidsforrequest — only BID - OPEN requests (active review UI).
_CUSTOMER_BID_REVIEW_STATUSES = frozenset({"BID - OPEN"})
# Selectable bids for customer acceptance (null-as-open not used — insert always sets BID - OPEN).
_SELECTABLE_BID_STATUSES = frozenset({"BID - OPEN"})


def _ist_now_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def _as_float_amount(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int_or_zero(value) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_optional_date(value):
    """Coerce DB date/datetime/string to date. Zero-dates and junk → None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text.startswith("0000-00-00"):
            return None
        parsed = parse_dob(text)
        if parsed is not None:
            return parsed
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt).date()
            except ValueError:
                continue
        return None
    return None


def _tag_names_from_csv(db: Session, tags_str) -> list:
    tag_ids = []
    if tags_str:
        for t in str(tags_str).split(","):
            cleaned = t.strip()
            if cleaned.isdigit():
                tag_ids.append(int(cleaned))
    if not tag_ids:
        return []
    tags_rows = db.query(Tag.tagsName).filter(Tag.TAGID.in_(tag_ids)).all()
    names = []
    for row in tags_rows:
        if row[0] is None:
            continue
        names.append(str(row[0]))
    return names


def _bidder_user_join():
    """Phone business id: biddetails.bidderID is integer, usertable.userAppId is string.

    Compare numerically so MySQL does not CAST(bigint AS CHAR) (collation /
    zero-pad mismatches → SQLAlchemyError → ERROR_PREPARE). SQLite CAST to
    BIGINT keeps PR10 in-memory tests matching.
    """
    return func.cast(User.userAppId, BigInteger) == BidDetail.bidderID


def get_bids_for_request(
    db: Session,
    rid: int,
    user_id: Optional[str] = None,
):
    """
    Customer-owned bid list for a request (PR10).

    Validation order: load request → 404 → ownership 403 → status gate →
    return selectable BID - OPEN bids only, sorted by amount ascending.
    Never returns FCMTOKEN.
    Empty result is ``[]`` (preferred). Intentionally does not enforce bidEndTime.
    """
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
                detail="Not authorized to view bids for this request",
            )

        if request_row.requestStatus not in _CUSTOMER_BID_REVIEW_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        bids = (
            db.query(
                BidDetail,
                User.fullName,
                User.rating,
                User.totalNoOfReviews,
                User.profilePicture,
                User.dob,
                func.cast(User.joiningDate, SAString),
                User.city,
                User.tags,
                User.noOfTripsCompleted,
                CarDetail.CARID,
                CarDetail.carRegNo,
                CarDetail.carModel,
                CarDetail.modelYear,
                CarDetail.carColor,
                CarDetail.ownerName,
                CarDetail.registeredOn,
                CarDetail.imageVehicleFront,
                CarDetail.imageVehicleSide,
                CarTypeDetail.car_type,
                CarTypeDetail.car_sub_type,
            )
            .join(User, _bidder_user_join())
            .outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID)
            .outerjoin(CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD)
            .filter(
                BidDetail.rID == rid,
                BidDetail.bidStatus.in_(list(_SELECTABLE_BID_STATUSES)),
            )
            .all()
        )

        if not bids:
            return []

        result = []

        for (
            bid,
            fullName,
            rating,
            totalNoOfReviews,
            profilePicture,
            dob,
            joiningDate,
            city,
            tags_str,
            noOfTripsCompleted,
            car_id,
            car_reg_no,
            car_model,
            model_year,
            car_color,
            owner_name,
            registered_on,
            image_vehicle_front,
            image_vehicle_side,
            car_type,
            car_sub_type,
        ) in bids:
            registered_on_str = None
            if registered_on is not None:
                if hasattr(registered_on, "strftime"):
                    registered_on_str = registered_on.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    text = str(registered_on)
                    registered_on_str = None if text.startswith("0000-00-00") else text

            result.append(
                CustomerBidDetail(
                    BIDID=bid.BID,
                    BIDDERID=str(bid.bidderID),
                    BIDAMOUNT=_as_float_amount(bid.bidAmount),
                    BIDSTATUS=bid.bidStatus,
                    BIDDERNAME=fullName,
                    BIDDERRATING=_as_float_amount(rating),
                    TOTALNOOFREVIEWS=_as_int_or_zero(totalNoOfReviews),
                    PROFILEPIC=profilePicture,
                    DOB=_as_optional_date(dob),
                    JOININGDATE=_as_optional_date(joiningDate),
                    BASELOCATION=city,
                    TAGS=_tag_names_from_csv(db, tags_str),
                    NOOFTRIPSCOMPLETED=_as_int_or_zero(noOfTripsCompleted),
                    CARID=car_id,
                    CARREGNO=car_reg_no,
                    CARMODEL=car_model,
                    MODELYEAR=str(model_year) if model_year is not None else None,
                    CARCOLOR=car_color,
                    OWNERNAME=owner_name,
                    REGISTEREDON=registered_on_str,
                    IMAGEVEHICLEFRONT=image_vehicle_front,
                    IMAGEVEHICLESIDE=image_vehicle_side,
                    CAR_TYPE=car_type,
                    CAR_SUB_TYPE=car_sub_type,
                )
            )

        result.sort(key=lambda item: (_as_float_amount(item.BIDAMOUNT), item.BIDID))
        return result
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        print(f"[get_bids_for_request] ERROR: {type(e).__name__}")
        return ErrorResponse(message="ERROR_PREPARE")
    except Exception as e:
        print(f"[get_bids_for_request] ERROR_EXCEPTION: {type(e).__name__}")
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
                Request.noOfBids: Request.noOfBids - 1
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
    
# def accept_bid(db:Session, rid :int, vendor_id :int):
#     try : 
#         requestupdate = db.query(Request).filter(Request.RID == rid).update({
#             Request.requestStatus:"BID - CONFIRMED",
#             Request.tableTimestamp:func.current_timestamp()
#         })        
#         if requestupdate==0:
#             return ErrorResponse(message="INSER ERROR IN FUNCTION")
        
#         bidupdate = db.query(BidDetail).filter((BidDetail.rID == rid)&(BidDetail.bidderID == vendor_id)).update({
#             BidDetail.bidStatus:"BID - CONFIRMED",
#             BidDetail.tableTimestamp:func.current_timestamp()
#         })
        
#         if bidupdate==0:
#             return ErrorResponse(message="NOT UPDATED")
#         db.commit()
#         return ErrorResponse(message="UPDATED")
#     except SQLAlchemyError:
#         db.rollback()
#         return ErrorResponse(message="ERROR")
#     finally:
#         db.close()

def accept_bid(
    db: Session,
    rid: int,
    bid_id: int,
    user_id: Optional[str] = None,
    background_tasks: Optional[BackgroundTasks] = None,
    notification_type: str = "default",
    actor_auth_subject: Optional[str] = None,
):
    """
    Customer accept bid (PR10) — identity is RID + BIDID only.

    Derives vendor, car, and amount from the bid row. Does not trust client
    nested maps. Does not enforce bidEndTime (intentional PHP compatibility).
    Does not set requestWonBy / finalAmount (vendor handshake still owns those).
    Competing bids are left unchanged.

    PR40: emits bid.accepted only on real BID - OPEN → BID - CONFIRMED transition.
    """

    should_notify = False
    vendor_to_notify = None

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
                detail="Not authorized to accept a bid for this request",
            )

        bid_row = db.query(BidDetail).filter(BidDetail.BID == bid_id).first()
        if not bid_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bid not found",
            )

        if bid_row.rID != rid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid does not belong to this request",
            )

        confirmed_bids = (
            db.query(BidDetail)
            .filter(
                BidDetail.rID == rid,
                BidDetail.bidStatus == "BID - CONFIRMED",
            )
            .all()
        )

        # Idempotent replay: already BID - CONFIRMED with same BIDID selected.
        if request_row.requestStatus == "BID - CONFIRMED":
            if len(confirmed_bids) > 1:
                print(
                    f"[accept_bid] integrity: multiple confirmed bids for RID={rid}"
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Conflicting confirmed bids",
                )
            if len(confirmed_bids) == 1 and confirmed_bids[0].BID == bid_id:
                # No mutation, no duplicate notification, no event.
                db.rollback()
                return ErrorResponse(message="UPDATED")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Request already has a different confirmed bid",
            )

        if request_row.requestStatus != "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if bid_row.bidStatus not in _SELECTABLE_BID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid is not selectable",
            )

        if confirmed_bids:
            # Should not happen while request is BID - OPEN; fail safe.
            print(
                f"[accept_bid] integrity: confirmed bid rows while BID - OPEN RID={rid}"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflicting confirmed bids",
            )

        now = _ist_now_naive()
        vendor_to_notify = str(bid_row.bidderID)

        request_updated = (
            db.query(Request)
            .filter(Request.RID == rid)
            .update(
                {
                    Request.requestStatus: "BID - CONFIRMED",
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

        bid_updated = (
            db.query(BidDetail)
            .filter(
                BidDetail.BID == bid_id,
                BidDetail.rID == rid,
            )
            .update(
                {
                    BidDetail.bidStatus: "BID - CONFIRMED",
                    BidDetail.tableTimestamp: now,
                },
                synchronize_session=False,
            )
        )
        if bid_updated == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid is not selectable",
            )

        # requestWonBy / finalAmount intentionally unchanged.
        maybe_append_domain_event(
            db,
            event_type=EVENT_BID_ACCEPTED,
            aggregate_id=str(rid),
            payload={
                "requestId": int(rid),
                "bidId": int(bid_id),
            },
            actor_auth_subject=actor_auth_subject,
        )

        should_notify = True

        db.commit()

        if should_notify and background_tasks is not None and vendor_to_notify:
            background_tasks.add_task(
                notify_vendor_bid_accepted,
                vendor_to_notify,
                notification_type,
            )

        return ErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[accept_bid] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[accept_bid] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
    
# def insert_bid(db: Session, bid_data : BidInsert):    
#     try :         
#         existing_bid = db.query(BidDetail).filter((BidDetail.rID == bid_data.RID) &
#                                                 (BidDetail.bidderID == bid_data.bidderID) &
#                                                 (BidDetail.CARID == bid_data.assignedVehicleID)).first()
#         if existing_bid:
#             return ErrorResponse(message="BID ALREADY PRESENT")
        
#         # Insert new bid
#         new_bid = BidDetail(
#             rID = bid_data.RID,
#             bidderID = bid_data.bidderID,
#             CARID = bid_data.assignedVehicleID,
#             bidAmount = bid_data.bidAmount,
#             bidStatus = "BID - OPEN",
#             tableTimestamp = datetime.utcnow()
#         )
#         db.add(new_bid)
        
#         # Update requestTable 
#         updated = db.query(Request).filter(Request.RID == bid_data.RID).update({
#             Request.noOfBids: Request.noOfBids + 1,
#             Request.tableTimestamp: func.current_timestamp()
#         })
        
#         if updated ==0 :
#             raise RuntimeError("UPDATE REQUEST TABLE FAILED")
        
#         db.commit()
#         return ErrorResponse(message="UPDATED")
#     except SQLAlchemyError as e:
#         db.rollback()
#         return ErrorResponse(message=f"ERROR: {e.__class__.__name__}")
#     finally:
#         db.close()


def insert_bid(db: Session, bid_data):
    """
    Production-safe version of insertBid():
    - duplicate check depends on whether CARID exists
    - insert bid + increment request count in one DB transaction
    - notifications happen after commit
    - returns INSERTED on success
    """
    tz = ZoneInfo("Asia/Kolkata")

    try:
        car_id = (
            bid_data.assignedVehicleID
            if getattr(bid_data, "assignedVehicleID", None) not in ("", None)
            else None
        )

        customer_app_id = None

        # -------------------------
        # 1) TRANSACTION
        # -------------------------
        with db.begin():
            # Duplicate check
            if car_id is not None:
                existing_bid = (
                    db.query(BidDetail)
                    .filter(
                        BidDetail.rID == bid_data.RID,
                        BidDetail.bidderID == bid_data.bidderID,
                        BidDetail.CARID == car_id,
                    )
                    .first()
                )
            else:
                existing_bid = (
                    db.query(BidDetail)
                    .filter(
                        BidDetail.rID == bid_data.RID,
                        BidDetail.bidderID == bid_data.bidderID,
                    )
                    .first()
                )

            if existing_bid:
                return ErrorResponse(message="BID ALREADY PRESENT")

            # Load request before update so we can notify customer later
            request_row = db.query(Request).filter(Request.RID == bid_data.RID).first()
            if not request_row:
                return ErrorResponse(message="INSER ERROR IN FUNCTION")

            customer_app_id = request_row.customerAppId

            # Insert bid
            new_bid = BidDetail(
                rID=bid_data.RID,
                bidderID=bid_data.bidderID,
                bidAmount=bid_data.bidAmount,
                CARID=car_id,
                tableTimestamp=datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
                bidStatus="BID - OPEN",
            )
            db.add(new_bid)

            # Increment bid count
            updated = (
                db.query(Request)
                .filter(Request.RID == bid_data.RID)
                .update(
                    {
                        Request.noOfBids: Request.noOfBids + 1,
                    },
                    synchronize_session=False,
                )
            )

            if updated == 0:
                raise ValueError("INSER ERROR IN FUNCTION")

        # -------------------------
        # 2) NOTIFICATIONS AFTER COMMIT
        # -------------------------
        try:
            # Notify other vendors who already bid on same request
            vendor_ids = get_vendors_who_bid_on_request(db, bid_data.RID)
            other_vendor_ids = [
                vid for vid in vendor_ids
                if str(vid).strip().lower() != str(bid_data.bidderID).strip().lower()
            ]

            if other_vendor_ids:
                send_notification_to_selected_users(
                    db,
                    FCMSendDrivers(
                        title="Someone Else Bid on Your Same Request!",
                        body="Check your bid now. Another driver also gave a price.",
                        url="Someone Else Bid on Your Same Request!",
                        type=getattr(bid_data, "type", "default"),
                        soundFile="normal_notification",
                        driverIds=other_vendor_ids,
                    ),
                )

            # Notify customer
            if customer_app_id:
                send_notification_to_user(
                    db,
                    FCMSend(
                        userAppId=customer_app_id,
                        title="New Bid on your Request",
                        body="A Vendor has made a New Bid on your Request. Check it.",
                        url="New Bid on Your Request",
                        type=getattr(bid_data, "type", "default"),
                        soundFile="normal_notification",
                        source=None,
                        destination=None,
                        travelDate=None,
                        pickupTime=None,
                    ),
                )
        except Exception:
            # best-effort notifications, same spirit as PHP
            pass

        return ErrorResponse(message="INSERTED")

    except ValueError as e:
        db.rollback()
        return ErrorResponse(message=str(e))

    except SQLAlchemyError as e:
        db.rollback()
        return ErrorResponse(message=f"ERROR: {e.__class__.__name__}")

    except Exception:
        db.rollback()
        return ErrorResponse(message="INSER ERROR IN FUNCTION")



def update_car_id_bid(db:Session, data : UpdateCarIdForBidRequest):
    try : 
        bid = db.query(BidDetail).filter(BidDetail.BID == data.BID).first()
        
        if not bid:
            return ErrorResponse(message="BID NOT FOUND")
        
        if bid.CARID == data.CARID:
            return ErrorResponse(message="SAME CARID")
        
        bid.CARID = data.CARID
        bid.tableTimestamp = func.current_timestamp()
        db.commit()
        
        return ErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    finally:
        db.close()