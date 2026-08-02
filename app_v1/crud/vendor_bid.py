"""
PR11 vendor bidding / handshake CRUD.

Customer PR10 paths remain in crud.bid / crud.request.cancel_handshake.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import String as SAString, cast, func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from ..models.bid_details import BidDetail
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..models.location_details import LocationDetail
from ..models.request_table import Request
from ..models.tags_table import Tag
from ..models.user_table import User
from ..schemas.bid_details import (
    VendorBidDetail,
    VendorBidInsert,
    BidAmountUpdate,
    VendorCarSummaryResponse,
    VendorRejectBody,
)
from ..services.notifications import (
    notify_customer_new_bid,
    notify_other_vendors_new_bid,
    notify_customer_vendor_accepted,
    notify_losing_vendors_trip_won,
    notify_customer_vendor_rejected,
    notify_vendors_bidding_reopened,
)
from ..utils.common import ErrorResponse

_SELECTABLE_BID_STATUSES = frozenset({"BID - OPEN"})
_VENDOR_GET_REQUEST_STATUSES = frozenset({"BID - OPEN"})
_HANDSHAKE_REQUEST_STATUS = "BID - CONFIRMED"
_HANDSHAKE_BID_STATUS = "BID - CONFIRMED"
_CONFIRMED_REQUEST_STATUS = "REQUEST - CONFIRMED"
_CONFIRMED_BID_STATUS = "REQUEST - CONFIRMED"


def _ist_now_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def _as_float_amount(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _csv_to_int_set(csv_value) -> set[int]:
    if not csv_value:
        return set()
    result: set[int] = set()
    for part in str(csv_value).split(","):
        cleaned = part.strip()
        if cleaned.isdigit():
            result.add(int(cleaned))
    return result


def _bidder_matches(bidder_id, user_app_id: str) -> bool:
    return str(bidder_id).strip() == str(user_app_id).strip()


def require_active_vendor(db: Session, user_id: str) -> User:
    """JWT sub must be an active, approved, unlocked vendor."""
    user = db.query(User).filter(User.userAppId == user_id).first()
    if (
        user is None
        or not bool(getattr(user, "alsoVendor", False))
        or not bool(getattr(user, "vendorApproved", False))
        or bool(getattr(user, "lockApp", False))
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized as an active vendor",
        )
    return user


def _vendor_has_bid_on_request(db: Session, rid: int, vendor_id: str) -> bool:
    return (
        db.query(BidDetail.BID)
        .filter(
            BidDetail.rID == rid,
            cast(BidDetail.bidderID, SAString) == str(vendor_id),
        )
        .first()
        is not None
    )


def _request_matches_vendor_open_feed(
    db: Session, vendor: User, request_row: Request
) -> bool:
    """
    Minimum equivalent of worker open_requests city + requestType eligibility.

    Coupling note (PR11): mirrors openbid-worker fetch_vendor_data QUERY1/QUERY2
    cityPreferences + requestTypePreferences + BID - OPEN. Region prefs are
    loaded but unused in the worker (parity preserved). Pickup-future is NOT
    enforced here — intentional, matching PR11 deadline non-enforcement;
    mutation gates use requestStatus only.
    """
    city_ids = _csv_to_int_set(getattr(vendor, "cityPreferences", None))
    rtype_ids = _csv_to_int_set(getattr(vendor, "requestTypePreferences", None))
    if not city_ids or not rtype_ids:
        return False

    if request_row.requestType is None or int(request_row.requestType) not in rtype_ids:
        return False

    from_loc = (request_row.fromLocation or "").strip()
    to_loc = (request_row.toLocation or "").strip()
    if not from_loc and not to_loc:
        return False

    location_rows = (
        db.query(LocationDetail.LID, LocationDetail.location)
        .filter(LocationDetail.location.in_([from_loc, to_loc]))
        .all()
    )
    matched_lids = {int(row.LID) for row in location_rows if row.LID is not None}
    return bool(matched_lids & city_ids)


def vendor_can_view_request_bids(
    db: Session, vendor: User, request_row: Request
) -> bool:
    """
    Eligible if:
    - open-feed city/requestType match, OR
    - vendor already has a bid on this RID (My Open Bids → View Bids path).
    """
    if _vendor_has_bid_on_request(db, request_row.RID, vendor.userAppId):
        return True
    return _request_matches_vendor_open_feed(db, vendor, request_row)


def _recompute_no_of_bids(db: Session, rid: int) -> int:
    """COUNT of all bid rows for RID (active lifecycle rows)."""
    count = (
        db.query(func.count(BidDetail.BID)).filter(BidDetail.rID == rid).scalar()
    ) or 0
    return int(count)


def _build_vendor_bid_details(db: Session, rid: int) -> list[VendorBidDetail]:
    bids = (
        db.query(
            BidDetail,
            User.fullName,
            User.rating,
            User.totalNoOfReviews,
            User.profilePicture,
            User.joiningDate,
            User.tags,
            CarDetail.CARID,
            CarDetail.carRegNo,
            CarDetail.carModel,
        )
        .join(User, User.userAppId == cast(BidDetail.bidderID, SAString))
        .outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID)
        .filter(
            BidDetail.rID == rid,
            BidDetail.bidStatus.in_(list(_SELECTABLE_BID_STATUSES)),
        )
        .all()
    )

    result: list[VendorBidDetail] = []
    for (
        bid,
        full_name,
        rating,
        total_reviews,
        profile_picture,
        joining_date,
        tags_str,
        car_id,
        car_reg_no,
        car_model,
    ) in bids:
        tag_ids: list[int] = []
        if tags_str:
            for t in tags_str.split(","):
                cleaned = t.strip()
                if cleaned.isdigit():
                    tag_ids.append(int(cleaned))

        tag_names: list[str] = []
        if tag_ids:
            tags_rows = db.query(Tag.tagsName).filter(Tag.TAGID.in_(tag_ids)).all()
            for row in tags_rows:
                tag_names.append(row[0])

        safe_rating = float(rating) if rating is not None else 0.0
        try:
            safe_rating = float(safe_rating)
        except (TypeError, ValueError):
            safe_rating = 0.0
        safe_reviews = int(total_reviews) if total_reviews is not None else 0

        result.append(
            VendorBidDetail(
                BIDID=bid.BID,
                BIDDERID=str(bid.bidderID),
                BIDAMOUNT=_as_float_amount(bid.bidAmount),
                BIDSTATUS=bid.bidStatus,
                BIDDERNAME=full_name,
                BIDDERRATING=safe_rating,
                TOTALNOOFREVIEWS=safe_reviews,
                PROFILEPIC=profile_picture,
                JOININGDATE=joining_date,
                TAGS=tag_names,
                CARID=car_id,
                CARREGNO=car_reg_no,
                CARMODEL=car_model,
            )
        )

    result.sort(key=lambda item: (_as_float_amount(item.BIDAMOUNT), item.BIDID))
    return result


def get_bids_for_request_for_vendor(db: Session, rid: int, user_id: str):
    """
    GET /getallbidsforrequestforvendor

    Auth order: JWT vendor → load RID → BID - OPEN → eligibility → BID - OPEN bids.
    Empty → []. Never returns FCMTOKEN. Does not enforce bidEndTime.
    """
    try:
        vendor = require_active_vendor(db, user_id)

        request_row = db.query(Request).filter(Request.RID == rid).first()
        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if request_row.requestStatus not in _VENDOR_GET_REQUEST_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if not vendor_can_view_request_bids(db, vendor, request_row):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view bids for this request",
            )

        return _build_vendor_bid_details(db, rid)
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        print(f"[get_bids_for_request_for_vendor] ERROR: {e}")
        return ErrorResponse(message="ERROR_PREPARE")
    except Exception as e:
        print(f"[get_bids_for_request_for_vendor] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()


def get_vendor_cars_for_bidding(
    db: Session,
    user_id: str,
    user_app_id: Optional[str] = None,
):
    """
    GET /viewcarsforvendor

    JWT sub is authoritative. Optional userAppId must match JWT or 403.
    Returns only admin-approved cars owned by the vendor. Empty → [].
    """
    try:
        vendor = require_active_vendor(db, user_id)

        if user_app_id is not None and str(user_app_id).strip() != str(user_id).strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view cars for another vendor",
            )

        rows = (
            db.query(CarDetail, CarTypeDetail.car_type)
            .outerjoin(CarTypeDetail, CarDetail.CTD == CarTypeDetail.CTD)
            .filter(
                CarDetail.userAppId == vendor.userAppId,
                CarDetail.adminApproved == True,  # noqa: E712
                CarDetail.isDeleted == False,  # noqa: E712
            )
            .order_by(CarDetail.registeredOn)
            .all()
        )

        if not rows:
            return []

        return [
            VendorCarSummaryResponse(
                CARID=car.CARID,
                CARREGNO=car.carRegNo,
                CARMODEL=car.carModel,
                VEHICLE_FRONT=car.imageVehicleFront,
                CAR_TYPE=car_type,
            )
            for car, car_type in rows
        ]
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        print(f"[get_vendor_cars_for_bidding] ERROR: {e}")
        return ErrorResponse(message="ERROR_PREPARE")
    except Exception as e:
        print(f"[get_vendor_cars_for_bidding] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()


def insert_vendor_bid(
    db: Session,
    bid_data: VendorBidInsert,
    user_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
):
    """
    POST /insertbid — JWT vendor, RID/CARID/bidAmount only.

    Duplicate RID+vendor+CARID → BID ALREADY PRESENT (200), no notify, no count change.
    noOfBids recomputed (not +1). Notifications after commit via BackgroundTasks.
    Does not enforce bidEndTime.
    """
    should_notify = False
    customer_app_id = None
    vendor_id = str(user_id)

    try:
        require_active_vendor(db, user_id)

        request_row = (
            db.query(Request)
            .filter(Request.RID == bid_data.RID)
            .with_for_update()
            .first()
        )
        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if request_row.requestStatus != "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        car = (
            db.query(CarDetail)
            .filter(CarDetail.CARID == bid_data.CARID)
            .with_for_update()
            .first()
        )
        if not car:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Car not found",
            )

        if bool(getattr(car, "isDeleted", False)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Car is not eligible",
            )

        if str(car.userAppId).strip() != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to bid with this car",
            )

        if not bool(car.adminApproved):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Car is not eligible",
            )

        # Duplicate: one active bid per RID + vendor + CARID
        existing = (
            db.query(BidDetail)
            .filter(
                BidDetail.rID == bid_data.RID,
                cast(BidDetail.bidderID, SAString) == vendor_id,
                BidDetail.CARID == bid_data.CARID,
            )
            .with_for_update()
            .first()
        )
        if existing:
            db.rollback()
            return ErrorResponse(message="BID ALREADY PRESENT")

        now = _ist_now_naive()
        # bidderID column is Integer in schema — store numeric phone when possible
        try:
            bidder_int = int(vendor_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized as an active vendor",
            )

        # Raw insert avoids ORM FK name mismatches (requestTable vs requesttable)
        # across MySQL/SQLite while preserving production behaviour.
        db.execute(
            text(
                """
                INSERT INTO biddetails
                    (rID, bidderID, CARID, bidAmount, bidStatus, tableTimestamp, last_updated)
                VALUES
                    (:rid, :bidder, :car, :amount, :status, :ts, :ts)
                """
            ),
            {
                "rid": bid_data.RID,
                "bidder": bidder_int,
                "car": bid_data.CARID,
                "amount": bid_data.bidAmount,
                "status": "BID - OPEN",
                "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        new_count = _recompute_no_of_bids(db, bid_data.RID)
        db.query(Request).filter(Request.RID == bid_data.RID).update(
            {
                Request.noOfBids: new_count,
                Request.tableTimestamp: now,
            },
            synchronize_session=False,
        )

        customer_app_id = request_row.customerAppId
        should_notify = True
        db.commit()

        if should_notify and background_tasks is not None:
            if customer_app_id:
                background_tasks.add_task(
                    notify_customer_new_bid,
                    str(customer_app_id),
                )
            background_tasks.add_task(
                notify_other_vendors_new_bid,
                bid_data.RID,
                vendor_id,
            )

        return ErrorResponse(message="INSERTED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[insert_vendor_bid] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[insert_vendor_bid] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()


def update_vendor_bid(
    db: Session,
    bid_id: int,
    body: BidAmountUpdate,
    user_id: str,
):
    """
    PUT /updatebid?BIDID= — amount only. No FCM. No vehicle change.
    """
    try:
        require_active_vendor(db, user_id)

        bid_row = (
            db.query(BidDetail)
            .filter(BidDetail.BID == bid_id)
            .with_for_update()
            .first()
        )
        if not bid_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bid not found",
            )

        if not _bidder_matches(bid_row.bidderID, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to update this bid",
            )

        bid_rid = bid_row.rID
        bid_status = bid_row.bidStatus
        db.expunge(bid_row)

        request_row = (
            db.query(Request)
            .filter(Request.RID == bid_rid)
            .with_for_update()
            .first()
        )
        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        if request_row.requestStatus != "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if bid_status not in _SELECTABLE_BID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_BID_STATUS",
            )

        now = _ist_now_naive()
        updated = (
            db.query(BidDetail)
            .filter(BidDetail.BID == bid_id)
            .update(
                {
                    BidDetail.bidAmount: body.bidAmount,
                    BidDetail.tableTimestamp: now,
                },
                synchronize_session=False,
            )
        )
        if updated == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bid not found",
            )
        db.commit()
        return ErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[update_vendor_bid] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[update_vendor_bid] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()


def delete_vendor_bid(db: Session, bid_id: int, user_id: str):
    """
    DELETE /deletebid?BIDID= — hard delete, recompute noOfBids. No FCM.
    Missing bid → 404 (not idempotent success).
    """
    try:
        require_active_vendor(db, user_id)

        bid_row = (
            db.query(BidDetail)
            .filter(BidDetail.BID == bid_id)
            .with_for_update()
            .first()
        )
        if not bid_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bid not found",
            )

        if not _bidder_matches(bid_row.bidderID, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this bid",
            )

        rid = bid_row.rID
        bid_status = bid_row.bidStatus
        db.expunge(bid_row)

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

        if request_row.requestStatus != "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if bid_status not in _SELECTABLE_BID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_BID_STATUS",
            )

        now = _ist_now_naive()
        deleted = (
            db.query(BidDetail)
            .filter(BidDetail.BID == bid_id)
            .delete(synchronize_session=False)
        )
        if deleted == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bid not found",
            )

        new_count = _recompute_no_of_bids(db, rid)
        db.query(Request).filter(Request.RID == rid).update(
            {
                Request.noOfBids: new_count,
                Request.tableTimestamp: now,
            },
            synchronize_session=False,
        )
        db.commit()
        return ErrorResponse(message="DELETED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[delete_vendor_bid] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[delete_vendor_bid] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()


def accept_request_by_vendor(
    db: Session,
    rid: int,
    bid_id: int,
    user_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
):
    """
    PUT /acceptrequestbyvendor?RID=&BIDID=

    Derives vendor/amount from JWT + selected bid. Competing bids unchanged.
    Selected bid status → REQUEST - CONFIRMED (parity). Idempotent same vendor/BIDID.
    """
    should_notify = False
    customer_app_id = None
    losing_vendor_ids: list[str] = []
    vendor_id = str(user_id)

    try:
        require_active_vendor(db, user_id)

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

        if not _bidder_matches(bid_row.bidderID, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to accept this request",
            )

        # Idempotent: already REQUEST - CONFIRMED with same vendor + BIDID
        if request_row.requestStatus == _CONFIRMED_REQUEST_STATUS:
            won_by = (
                str(request_row.requestWonBy).strip()
                if request_row.requestWonBy is not None
                else ""
            )
            if (
                won_by == vendor_id
                and bid_row.bidStatus == _CONFIRMED_BID_STATUS
                and _bidder_matches(bid_row.bidderID, user_id)
            ):
                db.rollback()
                return ErrorResponse(message="UPDATED")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Request already confirmed with a different outcome",
            )

        if request_row.requestStatus != _HANDSHAKE_REQUEST_STATUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if bid_row.bidStatus != _HANDSHAKE_BID_STATUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_BID_STATUS",
            )

        # Verify this is the selected confirmed bid for the request
        confirmed_bids = (
            db.query(BidDetail)
            .filter(
                BidDetail.rID == rid,
                BidDetail.bidStatus == _HANDSHAKE_BID_STATUS,
            )
            .all()
        )
        if len(confirmed_bids) != 1 or confirmed_bids[0].BID != bid_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid is not the selected confirmed bid",
            )

        final_amount = int(round(_as_float_amount(bid_row.bidAmount)))
        now = _ist_now_naive()

        # Capture losing vendors before status change
        other_bids = (
            db.query(BidDetail.bidderID)
            .filter(
                BidDetail.rID == rid,
                BidDetail.BID != bid_id,
            )
            .all()
        )
        losing_vendor_ids = []
        seen = set()
        for (bidder,) in other_bids:
            key = str(bidder).strip()
            if key and key != vendor_id and key not in seen:
                seen.add(key)
                losing_vendor_ids.append(key)

        customer_app_id = request_row.customerAppId

        updated_req = (
            db.query(Request)
            .filter(Request.RID == rid)
            .update(
                {
                    Request.requestStatus: _CONFIRMED_REQUEST_STATUS,
                    Request.requestWonBy: vendor_id,
                    Request.finalAmount: final_amount,
                    Request.tableTimestamp: now,
                },
                synchronize_session=False,
            )
        )
        if updated_req == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found",
            )

        updated_bid = (
            db.query(BidDetail)
            .filter(BidDetail.BID == bid_id, BidDetail.rID == rid)
            .update(
                {
                    BidDetail.bidStatus: _CONFIRMED_BID_STATUS,
                    BidDetail.tableTimestamp: now,
                },
                synchronize_session=False,
            )
        )
        if updated_bid == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid update failed",
            )

        should_notify = True
        db.commit()

        if should_notify and background_tasks is not None:
            if customer_app_id:
                background_tasks.add_task(
                    notify_customer_vendor_accepted,
                    str(customer_app_id),
                )
            if losing_vendor_ids:
                background_tasks.add_task(
                    notify_losing_vendors_trip_won,
                    losing_vendor_ids,
                )

        return ErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[accept_request_by_vendor] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[accept_request_by_vendor] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()


def reject_request_by_vendor_pr11(
    db: Session,
    rid: int,
    bid_id: int,
    body: VendorRejectBody,
    user_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
):
    """
    PUT /rejectrequestbyvendor?RID=&BIDID= + {rejectionReason}

    BID - CONFIRMED → BID - OPEN, hard-delete selected bid, recompute noOfBids.
    Default already-reopened → 409. No rejector self-notify.
    """
    should_notify = False
    customer_app_id = None
    vendor_id = str(user_id)
    reason = body.rejectionReason

    try:
        require_active_vendor(db, user_id)

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

        # Default PR11: already BID - OPEN → 409 (no durable reject evidence)
        if request_row.requestStatus == "BID - OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Request already reopened",
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

        if not _bidder_matches(bid_row.bidderID, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to reject this request",
            )

        if request_row.requestStatus != _HANDSHAKE_REQUEST_STATUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_REQUEST_STATUS",
            )

        if bid_row.bidStatus != _HANDSHAKE_BID_STATUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_BID_STATUS",
            )

        confirmed_bids = (
            db.query(BidDetail)
            .filter(
                BidDetail.rID == rid,
                BidDetail.bidStatus == _HANDSHAKE_BID_STATUS,
            )
            .all()
        )
        if len(confirmed_bids) != 1 or confirmed_bids[0].BID != bid_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid is not the selected confirmed bid",
            )

        customer_app_id = request_row.customerAppId
        now = _ist_now_naive()

        db.query(Request).filter(Request.RID == rid).update(
            {
                Request.requestStatus: "BID - OPEN",
                Request.rejectionReason: reason,
                Request.tableTimestamp: now,
            },
            synchronize_session=False,
        )

        deleted = (
            db.query(BidDetail)
            .filter(BidDetail.BID == bid_id, BidDetail.rID == rid)
            .delete(synchronize_session=False)
        )
        if deleted == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bid delete failed",
            )

        new_count = _recompute_no_of_bids(db, rid)
        db.query(Request).filter(Request.RID == rid).update(
            {
                Request.noOfBids: new_count,
                Request.tableTimestamp: now,
            },
            synchronize_session=False,
        )

        # requestWonBy / finalAmount remain unset (unchanged)
        should_notify = True
        db.commit()

        if should_notify and background_tasks is not None:
            if customer_app_id:
                background_tasks.add_task(
                    notify_customer_vendor_rejected,
                    str(customer_app_id),
                    reason,
                )
            background_tasks.add_task(
                notify_vendors_bidding_reopened,
                rid,
                vendor_id,
            )

        return ErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[reject_request_by_vendor_pr11] ERROR: {e}")
        return ErrorResponse(message="ERROR")
    except Exception as e:
        db.rollback()
        print(f"[reject_request_by_vendor_pr11] ERROR_EXCEPTION: {e}")
        return ErrorResponse(message="ERROR")
    finally:
        db.close()
