from fastapi import APIRouter, Depends, Query, BackgroundTasks
from sqlalchemy.orm import Session
from ..schemas.request_table import (RequestResponse,NoBidsResponse,RequestByRidResponse,UpdateResponse,RequestUpdate,
                                     RequestConfirmedForUserResponse,RequestConfirmedForVendorResponse,
                                     RequestCreate,AssignDriverRequest,RequestForUserResponse,
                                     RequestConfirmedCommonResponse,GetBookingReportResponse)
from ..schemas.request_type_details import RequestTypeBase
from ..utils.common import ErrorResponse,EmailErrorResponse
from typing import List, Union
from ..database import get_db
from ..crud.request import (get_all_open_requests,get_all_requests_for_user,get_rid_by_details,
                            get_booking_report,get_all_open_requests_for_vendor,get_request_type,
                            delete_request,update_request,accept_by_vendor,cancel_handshake,
                            booking_cancelled_by_user,get_all_confirmed_requests_for_customer,
                            get_all_confirmed_requests_for_vendor,reopen_request,
                            create_request,assign_driver_to_request,get_all_cancelled_requests_for_vendor,
                            reject_request_by_vendor,get_all_requests_by_request_status)
from ..auth.deps import get_current_user_id


router = APIRouter()

@router.get("/getallopenrequests",response_model=Union[List[RequestConfirmedCommonResponse],NoBidsResponse])
def get_open_requests(db: Session = Depends(get_db),
                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                      ):
    return get_all_open_requests(db)


@router.get("/getallrequestforuser",response_model=Union[List[RequestForUserResponse],NoBidsResponse])
def get_requests_user(customerAppId : str = Query(...), 
                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                      db:Session = Depends(get_db)):
    return get_all_requests_for_user(db,customer_app_id=customerAppId)


@router.get("/getridbydetails",response_model=Union[RequestByRidResponse, NoBidsResponse])
def get_rid_by_inputs(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
    fromLocation : str = Query(...),
    toLocation : str = Query(...),
    pickUpDate : str = Query(...),
    pickUpTime : str = Query(...),
    noOfAdults : int = Query(...),
    noOfKids : int = Query(...),
    carType : str = Query(...)
):
    return get_rid_by_details(db, fromLocation, toLocation, pickUpDate, pickUpTime, noOfAdults,noOfKids,carType)


@router.get("/getbookingreport",response_model=Union[List[GetBookingReportResponse],NoBidsResponse])

def get_booking_reports(
    db : Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
    startDate : str = Query(...),
    endDate : str = Query(...)    
):
    return get_booking_report(db, startDate, endDate)


@router.get("/getallopenbidsforvendor",response_model=Union[List[RequestConfirmedCommonResponse],NoBidsResponse])

def get_open_bids_vendor(db:Session = Depends(get_db), 
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         VENDORID: str=Query(...)):
    return get_all_open_requests_for_vendor(db,vendor_id=VENDORID)

@router.get("/getrequesttypes",response_model=Union[List[RequestTypeBase],ErrorResponse])

def read_all_request(db:Session=Depends(get_db),
                     user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                     ):
    return get_request_type(db)


@router.delete("/deleterequest", response_model=ErrorResponse)
def delete_request_route(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    RID: int = Query(...),
):
    # JWT sub is authoritative owner — ownership + BID - OPEN enforced in CRUD
    return delete_request(
        db, r_id=RID, background_tasks=background_tasks, user_id=user_id
    )


@router.put("/updaterequest", response_model=ErrorResponse)
def update_request_endpoint(
    requestdata: RequestUpdate,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    # JWT sub is authoritative owner — ownership + BID - OPEN enforced in CRUD
    return update_request(db, requestdata, user_id=user_id)

@router.put("/acceptrequestbyvendor",response_model=ErrorResponse)

def update_accept_request_by_vendor(db:Session=Depends(get_db), 
                                    user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                    VENDORID : int = Query(...), RID : int = Query(...), 
                                    FINALAMOUNT : float = Query(...)):
    return accept_by_vendor(db, rid=RID, vendor_id=VENDORID, final_amount=FINALAMOUNT)

@router.put("/rejectrequestbyvendor",response_model=ErrorResponse)
def reject_by_vendor(db:Session=Depends(get_db),
                     user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                     RID : int = Query(...), 
                     BID :int = Query(...), 
                     rejectionReason : str = Query(...)):
    return reject_request_by_vendor(db,rid=RID,bid_id=BID,rejection_reason=rejectionReason)

@router.put("/cancelhandshakerequest", response_model=ErrorResponse)

def cancel_handshake_of_request(db:Session=Depends(get_db),
                                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                RID : int = Query(...)):
    return cancel_handshake(db,rid=RID)

@router.put("/bookingcancelledbyuser",response_model=ErrorResponse)

def cancel_by_user(db:Session=Depends(get_db),
                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                   RID : int = Query(...), 
                   bidder_id : int = Query(...),
                   rejectionReason : str = Query(...)):
    return booking_cancelled_by_user(db,rid=RID,rejection_reason=rejectionReason)

@router.get("/getallconfirmedrequestsforuser",response_model=Union[List[RequestConfirmedForUserResponse],EmailErrorResponse])
def get_all_confirmed_customer_requests(db: Session = Depends(get_db),
                                        user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                        userAppId:str = Query(...)):
    return get_all_confirmed_requests_for_customer(db,user_app_id=userAppId)

@router.get("/getallconfirmedrequestsforvendor",response_model=Union[List[RequestConfirmedForVendorResponse],EmailErrorResponse])
def get_all_confirmed_vendor_requests(db: Session = Depends(get_db),
                                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                      vendorId:str = Query(...)):
    return get_all_confirmed_requests_for_vendor(db,vendor_id=vendorId)

@router.put("/reopenbooking",response_model=EmailErrorResponse)
def reopen_booking(backgroundTasks : BackgroundTasks, RID : str = Query(...), 
                   db:Session=Depends(get_db),
                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                   ):
    return reopen_request(db,r_id=RID, background_tasks=backgroundTasks)

@router.post("/insertrequest",response_model=EmailErrorResponse)
def create_new_request(create_data : RequestCreate, background_taks : BackgroundTasks, 
                       db:Session = Depends(get_db),
                       user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                       ):
    # JWT sub is authoritative customerAppId — ownership enforced inside create_request
    return create_request(db, create_data, background_taks, user_id=user_id)

@router.put("/updatedrivertorequest",response_model=EmailErrorResponse)
def driver_assign_to_request(request_data : AssignDriverRequest, db:Session=Depends(get_db),
                             user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                             ):
    return assign_driver_to_request(db,request_data)

@router.get("/getallcancelledrequestsforvendor",response_model=Union[List[RequestConfirmedForVendorResponse],EmailErrorResponse])
def get_all_vendor_cancelled_requests(db: Session = Depends(get_db),
                                      vendorId:str = Query(...),
                                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                      ):
    return get_all_cancelled_requests_for_vendor(db,vendor_id=vendorId)

@router.get("/getallrequestforuserbystatus",response_model=Union[List[RequestConfirmedCommonResponse],EmailErrorResponse])
def get_all_requests_by_request_status_endpoint(db:Session = Depends(get_db),
                                                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                                customerAppId:str = Query(...), requestStatus : str = Query(...)):
    return get_all_requests_by_request_status(db,customer_id=customerAppId,request_status=requestStatus)