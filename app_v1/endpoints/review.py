from fastapi import APIRouter, Depends, Query
from ..schemas.vendor_reviews import ReviewDetail,NoReviewResponse,ReviewCreate
from ..schemas.customer_reviews import CustomerReviewDetail,NoReviewResponse,CreateCustomerReview
from ..database import get_db
from sqlalchemy.orm import Session
from typing import Union,List
from ..crud.review import get_reviews_for_vendor,get_reviews_for_customer,create_vendor_review,insert_customer_review
from ..utils.common import ErrorResponse,EmailErrorResponse
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.get("/getallreviewsforvendor",response_model=Union[List[ReviewDetail],NoReviewResponse])
def get_reviews_vendor( db : Session = Depends(get_db),
                       user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                       VENDORID : str = Query(...)):
    return get_reviews_for_vendor(db,vendor_id=VENDORID)


@router.get("/getallreviewsforcustomer",response_model=Union[List[CustomerReviewDetail],NoReviewResponse])

def get_reviews_customer(db:Session = Depends(get_db), 
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         CUSTOMERID : str = Query(...)):
    return get_reviews_for_customer(db,customer_id=CUSTOMERID)

@router.post("/insertfeedback",response_model=ErrorResponse)
def insert_vendor_feedback(feedback_data : ReviewCreate, 
                           db : Session =Depends(get_db),
                           user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                           ):
    return create_vendor_review(db,feedback_data)

@router.post("/insertcustomerfeedback",response_model=EmailErrorResponse)
def create_customer_reivew(insert_data : CreateCustomerReview,  
                           db:Session = Depends(get_db),
                           user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                           ):
    return insert_customer_review(db,insert_data)

