"""PR19 review/rating CRUD — JWT ownership, RID-derived mutation identity.

Rules (approved):
* Vendor review list: public-safe for any authenticated user for an existing vendor
* Customer review list: JWT sub only (no CUSTOMERID)
* Mutations: lock request, verify relationship + lifecycle, insert, set directional
  flag, recalculate aggregate from review rows, commit once; snapshot after commit
* No client reviewer/target IDs accepted on mutations
* Do not close request-scoped sessions here
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.bid_details import BidDetail
from ..models.car_details import CarDetail
from ..models.customer_reviews import CustomerReview
from ..models.driver_details import DriverDetail
from ..models.request_table import Request
from ..models.user_table import User
from ..models.vendor_reviews import VendorReview
from ..schemas.customer_reviews import (
    CreateCustomerReview,
    CustomerReviewInsertResponse,
    CustomerReviewSummaryResponse,
)
from ..schemas.vendor_reviews import (
    ReviewCreate,
    ReviewInsertResponse,
    VendorReviewSummaryResponse,
)
from ..utils.vendor_snapshot_refresh import request_snapshot_refresh

logger = logging.getLogger(__name__)

STATUS_REQUEST_CONFIRMED = "REQUEST - CONFIRMED"
_CANCELLED_STATUS_MARKERS = ("CANCELLED", "CANCELED")
TZ = ZoneInfo("Asia/Kolkata")
_HALF = Decimal("0.5")
_TWO_PLACES = Decimal("0.01")
_MAX_REVIEW_TEXT = 1000


def _ist_now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def _round_aggregate(value: float) -> str:
    return str(
        Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    )


def _float_rating(value) -> float:
    if value is None:
        return 0.0
    return float(value)


def _vendor_id_as_int(vendor_app_id: str) -> int:
    token = (vendor_app_id or "").strip()
    if not token.isdigit():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TARGET_NOT_FOUND",
        )
    return int(token)


def _is_cancelled_status(request_status: Optional[str]) -> bool:
    normalized = (request_status or "").upper()
    return any(marker in normalized for marker in _CANCELLED_STATUS_MARKERS)


def _pickup_datetime(request_row: Request) -> datetime:
    pickup_date = request_row.pickUpDate
    pickup_time = request_row.pickUpTime or time(0, 0, 0)
    if isinstance(pickup_date, datetime):
        return pickup_date.replace(tzinfo=None)
    if not isinstance(pickup_date, date):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TRIP_NOT_ELIGIBLE",
        )
    return datetime.combine(pickup_date, pickup_time)


def _assert_trip_eligible(request_row: Request) -> None:
    if (request_row.requestStatus or "") != STATUS_REQUEST_CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TRIP_NOT_ELIGIBLE",
        )
    if _is_cancelled_status(request_row.requestStatus):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TRIP_NOT_ELIGIBLE",
        )
    if _pickup_datetime(request_row) >= _ist_now():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TRIP_NOT_ELIGIBLE",
        )


def _parse_half_rating(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_RATING",
        )
    try:
        if isinstance(value, str):
            token = value.strip()
            if not token:
                raise ValueError("empty")
            parsed = float(token)
        elif isinstance(value, Decimal):
            parsed = float(value)
        elif isinstance(value, (int, float)):
            parsed = float(value)
        else:
            raise ValueError("bad type")
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_RATING",
        ) from None

    if parsed < 0.5 or parsed > 5.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_RATING",
        )
    # Half-star steps only (0.5, 1.0, …, 5.0)
    if abs(parsed * 2 - round(parsed * 2)) > 1e-9:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_RATING",
        )
    return Decimal(str(parsed)).quantize(_HALF, rounding=ROUND_HALF_UP)


def _normalize_review_text(raw) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_REVIEW_TEXT",
        )
    text = raw.strip()
    if len(text) > _MAX_REVIEW_TEXT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_REVIEW_TEXT",
        )
    return text


def _recalculate_vendor_aggregate(db: Session, vendor_app_id: str) -> None:
    rows = (
        db.query(
            VendorReview.driverBehaviour,
            VendorReview.punctuality,
            VendorReview.carCondition,
            VendorReview.cleanliness,
        )
        .filter(VendorReview.VENDORID == _vendor_id_as_int(vendor_app_id))
        .all()
    )
    count = len(rows)
    if count == 0:
        avg = 0.0
    else:
        means = []
        for behaviour, punctuality, car_condition, cleanliness in rows:
            means.append(
                (
                    _float_rating(behaviour)
                    + _float_rating(punctuality)
                    + _float_rating(car_condition)
                    + _float_rating(cleanliness)
                )
                / 4.0
            )
        avg = sum(means) / count

    updated = (
        db.query(User)
        .filter(User.userAppId == vendor_app_id)
        .update(
            {
                User.rating: _round_aggregate(avg),
                User.totalNoOfReviews: count,
            },
            synchronize_session=False,
        )
    )
    if updated == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TARGET_NOT_FOUND",
        )


def _recalculate_customer_aggregate(db: Session, customer_app_id: str) -> None:
    rows = (
        db.query(CustomerReview.generalRating)
        .filter(CustomerReview.ratingReceiverUserAppId == customer_app_id)
        .all()
    )
    count = len(rows)
    if count == 0:
        avg = 0.0
    else:
        avg = sum(_float_rating(r[0]) for r in rows) / count

    updated = (
        db.query(User)
        .filter(User.userAppId == customer_app_id)
        .update(
            {
                User.customerRating: _round_aggregate(avg),
                User.totalCustomerReviews: count,
            },
            synchronize_session=False,
        )
    )
    if updated == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TARGET_NOT_FOUND",
        )


def get_reviews_for_vendor(
    db: Session, vendor_id: str
) -> List[VendorReviewSummaryResponse]:
    """Public-safe vendor reviews for an existing vendor. Empty → []."""
    vendor_app_id = (vendor_id or "").strip()
    if not vendor_app_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TARGET_NOT_FOUND",
        )

    vendor = db.query(User).filter(User.userAppId == vendor_app_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TARGET_NOT_FOUND",
        )

    try:
        vendor_key = _vendor_id_as_int(vendor_app_id)
    except HTTPException:
        # Non-numeric vendor ids cannot match BigInteger VENDORID column.
        return []

    try:
        reviews = (
            db.query(
                VendorReview,
                User.fullName,
                User.profilePicture,
                Request.fromLocation,
                Request.toLocation,
                Request.pickUpDate,
                CarDetail.carRegNo,
                CarDetail.carModel,
                DriverDetail.driverName,
            )
            .join(User, VendorReview.customerAppId == User.userAppId)
            .join(Request, Request.RID == VendorReview.RID)
            .outerjoin(
                BidDetail,
                (BidDetail.rID == VendorReview.RID)
                & (BidDetail.bidderID == VendorReview.VENDORID),
            )
            .outerjoin(CarDetail, CarDetail.CARID == BidDetail.CARID)
            .outerjoin(
                DriverDetail, DriverDetail.DDID == Request.driverAssignedID
            )
            .filter(VendorReview.VENDORID == vendor_key)
            .order_by(VendorReview.VRID.desc())
            .all()
        )
    except SQLAlchemyError:
        logger.exception("get_reviews_for_vendor query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to load vendor reviews",
        ) from None

    result: List[VendorReviewSummaryResponse] = []
    for (
        review,
        full_name,
        profile_picture,
        from_location,
        to_location,
        pickup_date,
        car_reg_no,
        car_model,
        driver_name,
    ) in reviews:
        result.append(
            VendorReviewSummaryResponse(
                reviewId=int(review.VRID),
                requestId=int(review.RID),
                travelDate=pickup_date,
                driverBehaviour=_float_rating(review.driverBehaviour),
                punctuality=_float_rating(review.punctuality),
                carCondition=_float_rating(review.carCondition),
                cleanliness=_float_rating(review.cleanliness),
                comments=review.comments or "",
                reviewerDisplayName=full_name,
                reviewerProfileImageUrl=profile_picture,
                fromLocation=from_location,
                toLocation=to_location,
                carRegNo=car_reg_no,
                carModel=car_model,
                driverName=driver_name,
            )
        )
    return result


def get_reviews_for_customer(
    db: Session, jwt_sub: str
) -> List[CustomerReviewSummaryResponse]:
    """JWT-owned passenger reviews only. Empty → []."""
    customer_id = (jwt_sub or "").strip()
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        # Join rating GIVER (vendor), not the receiver customer.
        rows = (
            db.query(
                CustomerReview,
                User.fullName,
                User.profilePicture,
                Request.fromLocation,
                Request.toLocation,
                Request.pickUpDate,
            )
            .join(
                User,
                CustomerReview.ratingGiverUserAppId == User.userAppId,
            )
            .outerjoin(Request, Request.RID == CustomerReview.RID)
            .filter(CustomerReview.ratingReceiverUserAppId == customer_id)
            .order_by(CustomerReview.CR.desc())
            .all()
        )
    except SQLAlchemyError:
        logger.exception("get_reviews_for_customer query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to load customer reviews",
        ) from None

    result: List[CustomerReviewSummaryResponse] = []
    for (
        review,
        full_name,
        profile_picture,
        from_location,
        to_location,
        pickup_date,
    ) in rows:
        try:
            request_id = int(str(review.RID).strip())
        except (TypeError, ValueError):
            request_id = 0
        result.append(
            CustomerReviewSummaryResponse(
                reviewId=int(review.CR),
                requestId=request_id,
                generalRating=_float_rating(review.generalRating),
                comments=review.comments or "",
                travelDate=pickup_date,
                fromLocation=from_location,
                toLocation=to_location,
                reviewerDisplayName=full_name,
                reviewerProfileImageUrl=profile_picture,
            )
        )
    return result


def create_vendor_review(
    db: Session, feedback_data: ReviewCreate, jwt_sub: str
) -> ReviewInsertResponse:
    """Customer rates vendor — RID-based identity, atomic aggregate update."""
    reviewer = (jwt_sub or "").strip()
    if not reviewer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    comments = _normalize_review_text(feedback_data.comments)
    driver_behaviour = _parse_half_rating(feedback_data.driverBehaviour)
    punctuality = _parse_half_rating(feedback_data.punctuality)
    car_condition = _parse_half_rating(feedback_data.carCondition)
    cleanliness = _parse_half_rating(feedback_data.cleanliness)
    target_vendor_id: Optional[str] = None

    try:
        request_row = (
            db.query(Request)
            .filter(Request.RID == feedback_data.RID)
            .with_for_update()
            .first()
        )
        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="REQUEST_NOT_FOUND",
            )

        if (request_row.customerAppId or "").strip() != reviewer:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized",
            )

        won_by = (request_row.requestWonBy or "").strip()
        if not won_by:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="TARGET_NOT_FOUND",
            )

        vendor = db.query(User).filter(User.userAppId == won_by).first()
        if not vendor:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="TARGET_NOT_FOUND",
            )
        target_vendor_id = won_by

        _assert_trip_eligible(request_row)

        existing = (
            db.query(VendorReview)
            .filter(VendorReview.RID == feedback_data.RID)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ALREADY_REVIEWED",
            )

        vendor_key = _vendor_id_as_int(won_by)
        new_feedback = VendorReview(
            customerAppId=reviewer,
            RID=feedback_data.RID,
            VENDORID=vendor_key,
            driverBehaviour=driver_behaviour,
            punctuality=punctuality,
            carCondition=car_condition,
            cleanliness=cleanliness,
            refreshments=False,
            comments=comments,
            tableTimestamp=_ist_now(),
        )
        db.add(new_feedback)
        db.flush()

        request_row.reviewDone = "Y"
        _recalculate_vendor_aggregate(db, won_by)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ALREADY_REVIEWED",
        ) from None
    except SQLAlchemyError:
        db.rollback()
        logger.exception("create_vendor_review failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to submit review",
        ) from None

    if target_vendor_id:
        # After commit only — failure must not roll back the review.
        request_snapshot_refresh(target_vendor_id, flag="Vendor")

    return ReviewInsertResponse(message="INSERTED")


def insert_customer_review(
    db: Session, create_data: CreateCustomerReview, jwt_sub: str
) -> CustomerReviewInsertResponse:
    """Vendor rates customer — RID-based identity, atomic aggregate update."""
    reviewer = (jwt_sub or "").strip()
    if not reviewer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    comments = _normalize_review_text(create_data.COMMENTS)
    rating = _parse_half_rating(create_data.RATING)
    target_customer_id: Optional[str] = None

    try:
        request_row = (
            db.query(Request)
            .filter(Request.RID == create_data.RID)
            .with_for_update()
            .first()
        )
        if not request_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="REQUEST_NOT_FOUND",
            )

        won_by = (request_row.requestWonBy or "").strip()
        if won_by != reviewer:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized",
            )

        customer_id = (request_row.customerAppId or "").strip()
        if not customer_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="TARGET_NOT_FOUND",
            )

        customer = db.query(User).filter(User.userAppId == customer_id).first()
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="TARGET_NOT_FOUND",
            )
        target_customer_id = customer_id

        _assert_trip_eligible(request_row)

        existing = (
            db.query(CustomerReview)
            .filter(CustomerReview.RID == str(create_data.RID))
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ALREADY_REVIEWED",
            )

        new_review = CustomerReview(
            RID=str(create_data.RID),
            ratingGiverUserAppId=reviewer,
            ratingReceiverUserAppId=customer_id,
            generalRating=rating,
            comments=comments,
            tableTimestamp=_ist_now(),
        )
        db.add(new_review)
        db.flush()

        request_row.customerReviewDone = "Y"
        _recalculate_customer_aggregate(db, customer_id)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ALREADY_REVIEWED",
        ) from None
    except SQLAlchemyError:
        db.rollback()
        logger.exception("insert_customer_review failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to submit review",
        ) from None

    if target_customer_id:
        request_snapshot_refresh(target_customer_id, flag="Customer")

    return CustomerReviewInsertResponse(message="INSERTED")
