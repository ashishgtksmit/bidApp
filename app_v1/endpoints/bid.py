from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query
from typing import Optional, Union, List

from sqlalchemy.orm import Session

from ..auth.deps import AuthenticatedUser, get_current_user
from ..crud.bid import (
    get_bids_for_request,
    accept_bid,
    update_car_id_bid,
)
from ..crud.vendor_bid import (
    get_bids_for_request_for_vendor,
    insert_vendor_bid,
    update_vendor_bid,
    delete_vendor_bid,
)
from ..database import get_db
from ..schemas.bid_details import (
    CustomerBidDetail,
    VendorBidDetail,
    VendorBidInsert,
    BidAmountUpdate,
    NoBidResponse,
    UpdateCarIdForBidRequest,
)
from ..utils.common import ErrorResponse

router = APIRouter()


@router.get(
    "/getallbidsforrequest",
    response_model=Union[List[CustomerBidDetail], NoBidResponse, ErrorResponse],
)
def get_all_bids(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
):
    """Customer-owned bid list (PR10). Empty → ``[]``. No FCMTOKEN."""
    user_id = current_user.user_app_id
    return get_bids_for_request(db, rid=RID, user_id=user_id)


@router.get(
    "/getallbidsforrequestforvendor",
    response_model=Union[List[VendorBidDetail], NoBidResponse, ErrorResponse],
)
def get_all_bids_for_vendor(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
):
    """
    Vendor-visible bid list (PR11).

    Active vendor + BID - OPEN + open-feed eligibility (or existing bid).
    Does not weaken customer GET ownership. Empty → []. No FCMTOKEN.
    Intentionally does not enforce bidEndTime.
    """
    user_id = current_user.user_app_id
    return get_bids_for_request_for_vendor(db, rid=RID, user_id=user_id)


@router.delete("/deletebid", response_model=ErrorResponse)
def delete_bid_endpoint(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    BIDID: int = Query(...),
):
    """Hard-delete own BID - OPEN bid. RID derived from bid row. No FCM."""
    user_id = current_user.user_app_id
    return delete_vendor_bid(db, bid_id=BIDID, user_id=user_id)


@router.delete("/deletebidwithbid", response_model=ErrorResponse, deprecated=True)
def delete_bids_legacy(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(None),
    BID: int = Query(None),
    BIDID: int = Query(None),
):
    """Legacy alias — BIDID preferred. RID ignored; ownership from JWT."""
    user_id = current_user.user_app_id
    bid_id = BIDID if BIDID is not None else BID
    if bid_id is None:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="BIDID is required",
        )
    return delete_vendor_bid(db, bid_id=bid_id, user_id=user_id)


@router.put("/updatebid", response_model=ErrorResponse)
def update_bid_endpoint(
    body: BidAmountUpdate,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    BIDID: int = Query(...),
):
    """Update own BID - OPEN bid amount. No FCM. No vehicle change."""
    user_id = current_user.user_app_id
    return update_vendor_bid(db, bid_id=BIDID, body=body, user_id=user_id)


@router.put("/updatebidwithbid", response_model=ErrorResponse, deprecated=True)
def update_bid_legacy(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    BIDID: int = Query(...),
    bidAmount: Optional[float] = Query(None),
    body: Optional[BidAmountUpdate] = Body(None),
):
    """Legacy alias — prefer PUT /updatebid with JSON body."""
    user_id = current_user.user_app_id
    amount = body.bidAmount if body is not None else bidAmount
    if amount is None:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bidAmount is required",
        )
    return update_vendor_bid(
        db,
        bid_id=BIDID,
        body=BidAmountUpdate(bidAmount=amount),
        user_id=user_id,
    )


@router.put("/acceptbid", response_model=ErrorResponse)
def accept_bid_by_customer(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
    BIDID: int = Query(...),
):
    """Accept identity is RID + BIDID. Vendor/car/amount derived server-side."""
    user_id = current_user.user_app_id
    return accept_bid(
        db,
        rid=RID,
        bid_id=BIDID,
        user_id=user_id,
        background_tasks=background_tasks,
    )


@router.post("/insertbid", response_model=ErrorResponse)
def bid_insert(
    bidData: VendorBidInsert,
    background_tasks: BackgroundTasks,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Place bid (PR11). Body: RID, CARID, bidAmount only.
    bidderID/bidStatus derived server-side. Intentionally does not enforce bidEndTime.
    """
    user_id = current_user.user_app_id
    return insert_vendor_bid(
        db,
        bid_data=bidData,
        user_id=user_id,
        background_tasks=background_tasks,
    )


@router.put("/updatecaridforbid", response_model=ErrorResponse)
def update_car_id_for_bid(
    data: UpdateCarIdForBidRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    user_id = current_user.user_app_id
    return update_car_id_bid(db, data)
