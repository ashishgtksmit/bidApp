from fastapi import APIRouter, Depends, Query
from ..schemas.car_type_details import CarType,CarTypeDetailResponse
from ..schemas.vendor_car_types import VendorCarTypeDetail
from ..schemas.car_details import (CarDetailsResponse,NoCarDetailsResponse,CarDetailsDelete,CarDetailsCreate,
                                   GetAllCarsResponse,UpdateCarApprovalStatusRequest,
                                   UploadCarDocumentRequest,UploadCarDocumentResponse)
from ..utils.common import ErrorResponse,EmailErrorResponse
from typing import List, Union
from sqlalchemy.orm import Session
from ..database import get_db
from ..crud.car import (get_all_car_types,get_vendor_car_types,get_approved_car_for_vendor,delete_car_by_id,insert_car_details,get_all_cars,update_car_approval_status,
                        upload_car_document_backend)
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.get("/cartypedetails", response_model=List[CarTypeDetailResponse])
def read_car_types(db:Session = Depends(get_db),
                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                   ):
    return get_all_car_types(db)


@router.get("/getallvendorcartypes", response_model=List[VendorCarTypeDetail])
def read_vendor_car_types(db:Session = Depends(get_db),
                          user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                          ):
    return get_vendor_car_types(db)


@router.get("/viewcarsforvendor", response_model=Union[List[CarDetailsResponse],NoCarDetailsResponse])
def read_all_cars_for_vendor(db:Session = Depends(get_db), 
                             user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                             userAppId : str = Query(...)):
    return get_approved_car_for_vendor(db, userapp_id=userAppId)

@router.delete("/deletecarfromprofile",response_model=Union[EmailErrorResponse,ErrorResponse])
def delete_car(delete_data : CarDetailsDelete, 
               user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
               db: Session = Depends(get_db)):
    return delete_car_by_id(db,delete_data)

@router.post("/addcartoprofile",response_model=EmailErrorResponse)
def add_new_car(create_data:CarDetailsCreate, 
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                db:Session=Depends(get_db)):
    return insert_car_details(db,create_data)

@router.get("/getallcars", response_model=Union[List[GetAllCarsResponse],NoCarDetailsResponse])
def get_all_cars_endpoint(db:Session = Depends(get_db), 
                             user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                             ):
    return get_all_cars(db)

@router.put("/updatecarapprovalstatus", response_model=ErrorResponse)
def update_car_approval_status_endpoint(update_data: UpdateCarApprovalStatusRequest, 
                                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                      db: Session = Depends(get_db)):
    return update_car_approval_status(db, update_data)


@router.post("/uploadcardocumentbackend",response_model=Union[UploadCarDocumentResponse,ErrorResponse])
def upload_car_document_endpoint(upload_data: UploadCarDocumentRequest,
                                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                db: Session = Depends(get_db)):
    return upload_car_document_backend(db, upload_data, user_id)    