from fastapi import APIRouter, Depends, Query, Request
from ..schemas.user_table import (NoUserResponse,BidderDetail,UserBankDetailsResponse,UserCreate,
                                  LogoutResponse,UserDelete,UserBankDetailsUpdate,UserImageUpload,
                                  VendorUpdate,VendorKycCreate,UpdateRequestTypeSelectionsRequest,
                                  UpdateRegionCitySelectionsRequest,GetUserDetailsResponse,
                                  RequestTypeResponse,CustomerListItem,GetAllVendorsWithUnapprovedResponse,
                                  AdminNumberResponse,UpdateVendorApprovalRequest,
                                  UpdateVendorLockAppStatusRequest,RejectUserRequest,
                                  UploadVendorDocumentRequest,UploadVendorDocumentResponse,
                                  VendorBankAccountSummaryResponse)
from ..schemas.request_table import CustomerBookingVendorDetail
from ..utils.common import ErrorResponse,EmailErrorResponse,SMSErrorResponse,ImageResponse
from typing import Union, List, Optional
from ..database import get_db
from sqlalchemy.orm import Session
from ..crud.user import (get_user_details,check_user,get_all_vendors,get_vendor_by_rid,
                         get_user_bank_details,fcm_token_update,logout_user,
                         delete_user,update_vendor_bank_details,profile_image_upload,get_users_all,vendor_update,
                         vendor_update_with_kyc,update_request_type_selections,update_region_city_selections,
                         get_request_type_selections,get_all_customers,get_all_vendors_with_unapproved,get_admin_number,
                         update_vendor_approved_status,update_vendor_lock_app_status,reject_user,upload_vendor_document_backend)
from ..utils.otp import send_otp_to_user
from ..utils.rate_limit import client_ip_from_request, enforce_rate_limit
from ..auth.deps import get_current_user_id
import os



router = APIRouter()

@router.get("/getallusers",response_model=Union[List[GetUserDetailsResponse],NoUserResponse])
def get_all_users(db:Session = Depends(get_db),
                  user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                  ):
    return get_users_all(db)


@router.get("/getuserdetails",response_model=Union[List[GetUserDetailsResponse],NoUserResponse])
def get_user(db:Session = Depends(get_db), 
             user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
             userAppId : str = Query(...)):
    return get_user_details(db,userAppId=userAppId)


@router.get("/checkregistereduser", response_model=Union[NoUserResponse, ErrorResponse])
def check_registered_user(
    request: Request,
    db: Session = Depends(get_db),
    userAppId: str = Query(...),
):
    """Public pre-login registration check (PR5). No JWT required."""
    limited = enforce_rate_limit(
        db,
        bucket_key=f"checkregistereduser:ip:{client_ip_from_request(request)}",
        max_hits=int(os.getenv("RATE_LIMIT_CHECK_USER_PER_IP", "60")),
        window_seconds=int(os.getenv("RATE_LIMIT_CHECK_USER_WINDOW_SECONDS", "60")),
    )
    if limited is not None:
        return limited
    limited = enforce_rate_limit(
        db,
        bucket_key=f"checkregistereduser:user:{userAppId}",
        max_hits=int(os.getenv("RATE_LIMIT_CHECK_USER_PER_APPID", "30")),
        window_seconds=int(os.getenv("RATE_LIMIT_CHECK_USER_WINDOW_SECONDS", "60")),
    )
    if limited is not None:
        return limited
    return check_user(db, user_app_id=userAppId)


@router.get("/getallvendors",response_model=Union[List[GetUserDetailsResponse],NoUserResponse])
def get_vendors(db:Session = Depends(get_db),
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                ):
    return get_all_vendors(db)

@router.get(
    "/getvendordetailsbyrid",
    response_model=List[CustomerBookingVendorDetail],
)
def get_vendor_rid(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    RID: int = Query(...),
):
    """
    Customer-owned confirmed booking vendor details (PR12).
    JWT ownership enforced; returns [] when no selected vendor relation.
    """
    return get_vendor_by_rid(db, rid=RID, user_id=user_id)


@router.get(
    "/getregisteredbankaccount",
    response_model=VendorBankAccountSummaryResponse,
)
def read_user_bank_account(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    userAppId: Optional[str] = Query(None),
):
    """PR17 — JWT-owned masked bank summary for active approved vendors."""
    return get_user_bank_details(db, user_id=user_id, user_app_id=userAppId)


@router.put("/fcmtokenupdate",response_model=ErrorResponse)
def user_fcm_token_udpate(db:Session=Depends(get_db), 
                          user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                          userAppId : int = Query(...), fcmToken : str = Query(...)):
    return fcm_token_update(db,user_app_id=userAppId,fcm_token=fcmToken)

# @router.post("/login",response_model=Union[LoginResponse,ErrorResponse])
# def login_user_endpoint(login_data:UserLogin, db:Session=Depends(get_db)):
#     return login_user(db,login_data)

@router.post("/logout",response_model=Union[LogoutResponse,ErrorResponse])
def user_logout(userAppId : str, fcmToken : str, 
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                db : Session = Depends(get_db)):
    return logout_user(db,user_app_id=userAppId,fcm_token=fcmToken,)

@router.post("/otpcall", response_model=Union[ErrorResponse, SMSErrorResponse])
def send_otp(
    request: Request,
    userAppId: str,
    db: Session = Depends(get_db),
):
    """Public OTP send (PR5). Stores OTP hash server-side; never returns OTP."""
    limited = enforce_rate_limit(
        db,
        bucket_key=f"otpcall:ip:{client_ip_from_request(request)}",
        max_hits=int(os.getenv("RATE_LIMIT_OTPCALL_PER_IP", "20")),
        window_seconds=int(os.getenv("RATE_LIMIT_OTPCALL_WINDOW_SECONDS", "900")),
    )
    if limited is not None:
        return limited
    limited = enforce_rate_limit(
        db,
        bucket_key=f"otpcall:user:{userAppId}",
        max_hits=int(os.getenv("RATE_LIMIT_OTPCALL_PER_APPID", "5")),
        window_seconds=int(os.getenv("RATE_LIMIT_OTPCALL_WINDOW_SECONDS", "900")),
    )
    if limited is not None:
        return limited
    return send_otp_to_user(db, user_app_id=userAppId)

@router.post("/deleteappuser",response_model=ErrorResponse)
def delete_existing_user(user_delete_data : UserDelete, 
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         db:Session = Depends(get_db)):
    return delete_user(db,user_delete_data)

@router.put("/updatevendorbankdetails", response_model=ErrorResponse)
def vendor_bank_details_update(
    user_data: UserBankDetailsUpdate,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """PR17 — JWT-owned bank text update for active approved vendors."""
    return update_vendor_bank_details(db, user_data, user_id=user_id)

@router.post("/profilepageupload",response_model=Union[EmailErrorResponse,ImageResponse,ErrorResponse])
def upload_profile_image(image_data : UserImageUpload, 
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         db: Session = Depends(get_db) ):
    return profile_image_upload(db,image_data)

@router.put("/alsovendorupdate",response_model=EmailErrorResponse)
def also_vendor_update(vendor_data : VendorUpdate,
                       user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                       db:Session = Depends(get_db)):
    return vendor_update(db,vendor_data)

@router.put("/registernewvendor",response_model=EmailErrorResponse)
def register_new_vendor(vendor_data :VendorKycCreate, 
                        user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                        db:Session = Depends(get_db)):
    return vendor_update_with_kyc(db, vendor_data, user_id)

@router.put("/updaterequesttypeselections", response_model=ErrorResponse)
def update_request_type_selections_endpoint(
    data: UpdateRequestTypeSelectionsRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return update_request_type_selections(db, data, user_id)


@router.put("/updateregioncityselections", response_model=ErrorResponse)
def update_region_city_selections_endpoint(
    data: UpdateRegionCitySelectionsRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return update_region_city_selections(db, data, user_id)


@router.get(
    "/getuserrequesttypepreferences",
    response_model=List[RequestTypeResponse],
)
def get_user_request_type_preferences(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    userAppId: Optional[str] = Query(None),
):
    return get_request_type_selections(db, user_id=user_id, user_app_id=userAppId)


@router.get("/getallcustomers",response_model=Union[List[CustomerListItem],NoUserResponse])
def get_all_customers_(db:Session = Depends(get_db),
                  user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                  ):
    return get_all_customers(db)

@router.get("/getallvendorswithunapproved",response_model=Union[List[GetUserDetailsResponse],NoUserResponse])
def get_all_vendors_with_unapproved_(db:Session = Depends(get_db),
                    user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                    ):
        return get_all_vendors_with_unapproved(db)  

@router.get("/getadminnumber",response_model=Union[AdminNumberResponse,NoUserResponse,EmailErrorResponse])
def get_admin_number_endpoint(db:Session = Depends(get_db),
                                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                ):
    return get_admin_number(db)


@router.put("/updatevendorapprovedstatus",response_model=EmailErrorResponse)
def update_vendor_approved_status_endpoint(data : UpdateVendorApprovalRequest,
                                            user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                            db: Session = Depends(get_db) ):
    return update_vendor_approved_status(db,data)


@router.put("/updatevendorlockappstatus",response_model=EmailErrorResponse)
def update_vendor_lock_app_status_endpoint(data : UpdateVendorLockAppStatusRequest,
                                            user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                            db: Session = Depends(get_db) ):
    return update_vendor_lock_app_status(db,data)


@router.post("/rejectuser",response_model=EmailErrorResponse)

def reject_user_endpoint(data : RejectUserRequest,
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         db: Session = Depends(get_db) ):
    return reject_user(db,data)


@router.post("/uploadvendordocumentbackend",response_model=Union[UploadVendorDocumentResponse,EmailErrorResponse])

def upload_vendor_document_endpoint(data : UploadVendorDocumentRequest,
                                    user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                    db: Session = Depends(get_db) ):
        return upload_vendor_document_backend(db,data)  