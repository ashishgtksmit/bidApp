from typing import List

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ..auth.deps import get_current_user_id
from ..crud.review import (
    create_vendor_review,
    get_reviews_for_customer,
    get_reviews_for_vendor,
    insert_customer_review,
)
from ..database import get_db
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

router = APIRouter()


@router.get(
    "/getallreviewsforvendor",
    response_model=List[VendorReviewSummaryResponse],
)
def get_reviews_vendor(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    VENDORID: str = Query(...),
):
    """Public-safe vendor reviews for an existing vendor (any authenticated user)."""
    _ = user_id  # JWT gate only — vendor reviews are intentionally public-safe
    return get_reviews_for_vendor(db, vendor_id=VENDORID)


@router.get(
    "/getallreviewsforcustomer",
    response_model=List[CustomerReviewSummaryResponse],
)
def get_reviews_customer(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """JWT-owned passenger reviews. CUSTOMERID query param is not accepted."""
    return get_reviews_for_customer(db, jwt_sub=user_id)


@router.post(
    "/insertfeedback",
    response_model=ReviewInsertResponse,
    status_code=status.HTTP_201_CREATED,
)
def insert_vendor_feedback(
    feedback_data: ReviewCreate,
    response: Response,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Customer rates the winning vendor for an eligible completed trip."""
    result = create_vendor_review(db, feedback_data, jwt_sub=user_id)
    response.status_code = status.HTTP_201_CREATED
    return result


@router.post(
    "/insertcustomerfeedback",
    response_model=CustomerReviewInsertResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer_review(
    insert_data: CreateCustomerReview,
    response: Response,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Vendor rates the passenger for an eligible completed trip."""
    result = insert_customer_review(db, insert_data, jwt_sub=user_id)
    response.status_code = status.HTTP_201_CREATED
    return result
