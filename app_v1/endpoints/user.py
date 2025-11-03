from fastapi import APIRouter, Depends, Query
from ..schemas.user_table import (NoUserResponse,BidderDetail,UserBankDetailsResponse,UserCreate,
                                  LogoutResponse,UserDelete,UserBankDetailsUpdate,UserImageUpload,
                                  VendorUpdate,VendorKycCreate,UpdateRequestTypeSelectionsRequest,
                                  UpdateRegionCitySelectionsRequest,GetUserDetailsResponse,RequestTypeResponse)
from ..utils.common import ErrorResponse,EmailErrorResponse,SMSErrorResponse,ImageResponse
from typing import Union, List
from ..database import get_db
from sqlalchemy.orm import Session
from ..crud.user import (get_user_details,check_user,get_all_vendors,get_vendor_by_rid,
                         get_user_bank_details,update_password,fcm_token_update,insert_user,logout_user,
                         delete_user,update_vendor_bank_details,profile_image_upload,get_users_all,vendor_update,
                         vendor_update_with_kyc,update_request_type_selections,update_region_city_selections,
                         get_request_type_selections)
from ..utils.otp import send_otp_to_user
from ..auth.deps import get_current_user_id



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


@router.get("/checkregistereduser",response_model=NoUserResponse)
def check_registered_user(db:Session = Depends(get_db), 
                          user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                          userAppId : str = Query(...)):
    return check_user(db,user_app_id=userAppId)


@router.get("/getallvendors",response_model=Union[List[GetUserDetailsResponse],NoUserResponse])
def get_vendors(db:Session = Depends(get_db),
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                ):
    return get_all_vendors(db)

@router.get("/getvendordetailsbyrid",response_model=Union[List[BidderDetail],NoUserResponse])
def get_vendor_rid(db:Session = Depends(get_db), 
                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                   RID : str = Query(...)):
    return get_vendor_by_rid(db,rid=RID)


@router.get("/getregisteredbankaccount", response_model=Union[UserBankDetailsResponse,ErrorResponse])
def read_user_bank_account(db:Session = Depends(get_db), 
                           user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                           userAppId : str = Query(...)):
    return get_user_bank_details(db,userappid=userAppId)

@router.put("/updatepassword",response_model=ErrorResponse)
def user_update_password(db:Session=Depends(get_db), 
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         userAppId : int = Query(...), password : str = Query(...)):
    return update_password(db,user_app_id=userAppId,password=password)

@router.put("/fcmtokenupdate",response_model=ErrorResponse)
def user_fcm_token_udpate(db:Session=Depends(get_db), 
                          user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                          userAppId : int = Query(...), fcmToken : str = Query(...)):
    return fcm_token_update(db,user_app_id=userAppId,fcm_token=fcmToken)

@router.post("/insertuser",response_model=ErrorResponse)
def create_user(user_data:UserCreate, db:Session=Depends(get_db),
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                ):
    return insert_user(db,user_data)

# @router.post("/login",response_model=Union[LoginResponse,ErrorResponse])
# def login_user_endpoint(login_data:UserLogin, db:Session=Depends(get_db)):
#     return login_user(db,login_data)

@router.post("/logout",response_model=Union[LogoutResponse,ErrorResponse])
def user_logout(userAppId : str, fcmToken : str, 
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                db : Session = Depends(get_db)):
    return logout_user(db,user_app_id=userAppId,fcm_token=fcmToken,)

@router.post("/otpcall",response_model=Union[ErrorResponse,SMSErrorResponse])
def send_otp(userAppId: str, 
             user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
             db : Session = Depends(get_db)):
    return send_otp_to_user(db,user_app_id=userAppId)

@router.post("/deleteappuser",response_model=ErrorResponse)
def delete_existing_user(user_delete_data : UserDelete, 
                         user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         db:Session = Depends(get_db)):
    return delete_user(db,user_delete_data)

@router.put("/updatevendorbankdetails",response_model=ErrorResponse)
def vendor_bank_details_update(user_data : UserBankDetailsUpdate, 
                               user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                               db:Session = Depends(get_db)):
    return update_vendor_bank_details(db,user_data)

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
    return vendor_update_with_kyc(db,vendor_data)

@router.put("/updaterequesttypeselections",response_model=EmailErrorResponse)
def update_request_type_selections_endpoint(data :UpdateRequestTypeSelectionsRequest, 
                                            user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                            db: Session = Depends(get_db) ):
    return update_request_type_selections(db,data)

@router.put("/updateregioncityselections",response_model=EmailErrorResponse)
def update_region_city_selections_endpoint(data :UpdateRegionCitySelectionsRequest, 
                                           user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                           db: Session = Depends(get_db) ):
    return update_region_city_selections(db,data)

@router.get("/getuserrequesttypepreferences",response_model=Union[List[RequestTypeResponse],EmailErrorResponse])
def get_user_request_type_preferences(db: Session = Depends(get_db),
                                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                      userAppId:str=Query(...)):
    return get_request_type_selections(db,user_app_id=userAppId)

    


