from fastapi import APIRouter, Depends, Query, BackgroundTasks
from sqlalchemy.orm import Session
from ..schemas.request_table import (RequestResponse,NoBidsResponse,RequestByRidResponse,UpdateResponse,RequestUpdate,
                                     RequestConfirmedForUserResponse,RequestConfirmedForVendorResponse,
                                     RequestCreate,AssignDriverRequest,RequestForUserResponse,
                                     RequestConfirmedCommonResponse,GetBookingReportResponse,
                                     CancelBookingBody, ReopenBookingResponse)
from ..schemas.booking_history import (
    CustomerBookingHistoryItem,
    VendorBookingHistoryItem,
    VendorCancelledHistoryItem,
)
from ..schemas.request_type_details import RequestTypeBase
from ..schemas.bid_details import VendorRejectBody
from ..utils.common import ErrorResponse,EmailErrorResponse
from typing import List, Optional, Union
from ..database import get_db
from ..crud.request import (get_all_open_requests,get_all_requests_for_user,get_rid_by_details,
                            get_booking_report,get_all_open_requests_for_vendor,get_request_type,
                            delete_request,update_request,cancel_handshake,
                            booking_cancelled_by_user,get_all_confirmed_requests_for_customer,
                            get_all_confirmed_requests_for_vendor,reopen_request,
                            create_request,assign_driver_to_request,get_all_cancelled_requests_for_vendor,
                            get_all_requests_by_request_status)
from ..crud.vendor_bid import accept_request_by_vendor, reject_request_by_vendor_pr11
from ..auth.deps import AuthenticatedUser, get_current_user


router = APIRouter()

@router.get("/getallopenrequests",response_model=Union[List[RequestConfirmedCommonResponse],NoBidsResponse])
def get_open_requests(db: Session = Depends(get_db),
                      current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                      ):
    user_id = current_user.user_app_id
    return get_all_open_requests(db)


@router.get(
    "/getallrequestforuser",
    response_model=List[CustomerBookingHistoryItem],
    summary="Customer completed booking history (PR20)",
)
def get_requests_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    customerAppId: Optional[str] = Query(
        None,
        description="Deprecated transitional identity. Must match JWT sub if sent.",
        deprecated=True,
    ),
):
    """
    Past REQUEST - CONFIRMED bookings owned by JWT sub.

    Flutter must not send customerAppId. Optional mismatch → 403.
    Empty history → [].
    """
    user_id = current_user.user_app_id
    return get_all_requests_for_user(
        db,
        user_id=user_id,
        customer_app_id=customerAppId,
    )


@router.get("/getridbydetails",response_model=Union[RequestByRidResponse, NoBidsResponse])
def get_rid_by_inputs(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
    fromLocation : str = Query(...),
    toLocation : str = Query(...),
    pickUpDate : str = Query(...),
    pickUpTime : str = Query(...),
    noOfAdults : int = Query(...),
    noOfKids : int = Query(...),
    carType : str = Query(...)
):
    user_id = current_user.user_app_id
    return get_rid_by_details(db, fromLocation, toLocation, pickUpDate, pickUpTime, noOfAdults,noOfKids,carType)


@router.get("/getbookingreport",response_model=Union[List[GetBookingReportResponse],NoBidsResponse])

def get_booking_reports(
    db : Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
    startDate : str = Query(...),
    endDate : str = Query(...)    
):
    user_id = current_user.user_app_id
    return get_booking_report(db, startDate, endDate)


@router.get("/getallopenbidsforvendor",response_model=Union[List[RequestConfirmedCommonResponse],NoBidsResponse])

def get_open_bids_vendor(db:Session = Depends(get_db), 
                         current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                         VENDORID: str=Query(...)):
    user_id = current_user.user_app_id
    return get_all_open_requests_for_vendor(db,vendor_id=VENDORID)

@router.get("/getrequesttypes",response_model=Union[List[RequestTypeBase],ErrorResponse])

def read_all_request(db:Session=Depends(get_db),
                     current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                     ):
    user_id = current_user.user_app_id
    return get_request_type(db)


@router.delete("/deleterequest", response_model=ErrorResponse)
def delete_request_route(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
):
    user_id = current_user.user_app_id
    # JWT sub is authoritative owner — ownership + BID - OPEN enforced in CRUD
    return delete_request(
        db, r_id=RID, background_tasks=background_tasks, user_id=user_id
    )


@router.put("/updaterequest", response_model=ErrorResponse)
def update_request_endpoint(
    requestdata: RequestUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = current_user.user_app_id
    # JWT sub is authoritative owner — ownership + BID - OPEN enforced in CRUD
    return update_request(db, requestdata, user_id=user_id)

@router.put("/acceptrequestbyvendor", response_model=ErrorResponse)
def update_accept_request_by_vendor(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
    BIDID: int = Query(...),
):
    """
    Vendor accept handshake (PR11). RID + BIDID only.
    Vendor/finalAmount derived from JWT + selected bid. No winner self-notify.
    """
    user_id = current_user.user_app_id
    return accept_request_by_vendor(
        db,
        rid=RID,
        bid_id=BIDID,
        user_id=user_id,
        background_tasks=background_tasks,
        actor_auth_subject=current_user.auth_subject,
    )


@router.put("/rejectrequestbyvendor", response_model=ErrorResponse)
def reject_by_vendor(
    background_tasks: BackgroundTasks,
    body: VendorRejectBody,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
    BIDID: int = Query(...),
):
    """
    Vendor reject handshake (PR11). RID + BIDID + rejectionReason body.
    Reopens to BID - OPEN; hard-deletes selected bid; recompute noOfBids.
    """
    user_id = current_user.user_app_id
    return reject_request_by_vendor_pr11(
        db,
        rid=RID,
        bid_id=BIDID,
        body=body,
        user_id=user_id,
        background_tasks=background_tasks,
        actor_auth_subject=current_user.auth_subject,
    )

@router.put("/cancelhandshakerequest", response_model=ErrorResponse)
def cancel_handshake_of_request(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
):
    """Customer cancel handshake. Ownership + status gate. No FCM in PR10."""
    user_id = current_user.user_app_id
    return cancel_handshake(
        db,
        rid=RID,
        user_id=user_id,
        actor_auth_subject=current_user.auth_subject,
    )

@router.put("/bookingcancelledbyuser", response_model=ErrorResponse)
def cancel_by_user(
    body: CancelBookingBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    RID: int = Query(...),
):
    """
    Customer confirmed-booking cancellation (PR12).

    Query: RID. Body: rejectionReason only.
    JWT sub is authoritative; vendor notify from request.requestWonBy.
    """
    user_id = current_user.user_app_id
    return booking_cancelled_by_user(
        db,
        rid=RID,
        rejection_reason=body.rejectionReason,
        user_id=user_id,
        background_tasks=background_tasks,
        actor_auth_subject=current_user.auth_subject,
    )

@router.get("/getallconfirmedrequestsforuser",response_model=Union[List[RequestConfirmedForUserResponse],EmailErrorResponse])
def get_all_confirmed_customer_requests(db: Session = Depends(get_db),
                                        current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                                        userAppId:str = Query(...)):
    user_id = current_user.user_app_id
    return get_all_confirmed_requests_for_customer(db,user_app_id=userAppId)

@router.get(
    "/getallconfirmedrequestsforvendor",
    response_model=List[VendorBookingHistoryItem],
    summary="Vendor completed trip history (PR20)",
)
def get_all_confirmed_vendor_requests(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    vendorId: Optional[str] = Query(
        None,
        description="Deprecated transitional identity. Must match JWT sub if sent.",
        deprecated=True,
    ),
):
    """
    Past REQUEST - CONFIRMED trips won by JWT sub (requestWonBy).

    Flutter must not send vendorId. Optional mismatch → 403.
    Empty history → [].
    """
    user_id = current_user.user_app_id
    return get_all_confirmed_requests_for_vendor(
        db,
        user_id=user_id,
        vendor_id=vendorId,
    )

@router.put("/reopenbooking", response_model=ReopenBookingResponse)
def reopen_booking(
    backgroundTasks: BackgroundTasks,
    RID: int = Query(...),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Reopen cancelled booking (PR12): clone new BID - OPEN request; mark original reopened.
    """
    user_id = current_user.user_app_id
    return reopen_request(
        db,
        r_id=RID,
        background_tasks=backgroundTasks,
        user_id=user_id,
    )

@router.post("/insertrequest",response_model=EmailErrorResponse)
def create_new_request(create_data : RequestCreate, background_taks : BackgroundTasks, 
                       db:Session = Depends(get_db),
                       current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                       ):
    user_id = current_user.user_app_id
    # JWT sub is authoritative customerAppId — ownership enforced inside create_request
    return create_request(db, create_data, background_taks, user_id=user_id)

@router.put("/updatedrivertorequest", response_model=EmailErrorResponse)
def driver_assign_to_request(
    request_data: AssignDriverRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Vendor driver assignment (PR13).

    Body: RID + DRIVERID only. JWT sub must own the request (requestWonBy)
    and the driver. Status gate: REQUEST - CONFIRMED.
    """
    user_id = current_user.user_app_id
    return assign_driver_to_request(
        db,
        request_data,
        user_id=user_id,
        background_tasks=background_tasks,
        actor_auth_subject=current_user.auth_subject,
    )

@router.get(
    "/getallcancelledrequestsforvendor",
    response_model=List[VendorCancelledHistoryItem],
    summary="Vendor cancelled trip history (PR21)",
)
def get_all_vendor_cancelled_requests(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    vendorId: Optional[str] = Query(
        None,
        description="Deprecated transitional identity. Must match JWT sub if sent.",
        deprecated=True,
    ),
):
    """
    Past BOOKING - CANCELLED BY USER trips won by JWT sub (requestWonBy).

    Flutter must not send vendorId. Optional mismatch → 403.
    Empty history → []. Current/future cancellations remain on WSS.
    """
    user_id = current_user.user_app_id
    return get_all_cancelled_requests_for_vendor(
        db,
        user_id=user_id,
        vendor_id=vendorId,
    )

@router.get("/getallrequestforuserbystatus",response_model=Union[List[RequestConfirmedCommonResponse],EmailErrorResponse])
def get_all_requests_by_request_status_endpoint(db:Session = Depends(get_db),
                                                current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                                                customerAppId:str = Query(...), requestStatus : str = Query(...)):
    user_id = current_user.user_app_id
    return get_all_requests_by_request_status(db,customer_id=customerAppId,request_status=requestStatus)