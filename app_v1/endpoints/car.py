from fastapi import APIRouter, Depends, Query
from ..schemas.car_type_details import CarTypeDetailResponse
from ..schemas.vendor_car_types import VendorCarTypeDetail
from ..schemas.car_details import (
    NoCarDetailsResponse,
    CarDetailsDelete,
    GetAllCarsResponse,
    UpdateCarApprovalStatusRequest,
    UploadCarDocumentRequest,
    UploadCarDocumentResponse,
    VendorManagedCar,
    CreateVendorCarRequest,
    DeleteVendorCarRequest,
)
from ..schemas.bid_details import VendorCarSummaryResponse
from ..utils.common import ErrorResponse,EmailErrorResponse
from typing import List, Optional, Union
from sqlalchemy.orm import Session
from ..database import get_db
from ..crud.car import (
    get_all_car_types,
    delete_car_by_id,
    get_all_cars,
    update_car_approval_status,
    upload_car_document_backend,
)
from ..crud.car_manage import (
    get_managed_cars_for_vendor,
    get_vendor_car_types_for_vendor,
    insert_car_for_vendor,
    delete_car_for_vendor,
)
from ..crud.vendor_bid import get_vendor_cars_for_bidding
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.get("/cartypedetails", response_model=List[CarTypeDetailResponse])
def read_car_types(db:Session = Depends(get_db),
                   user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                   ):
    return get_all_car_types(db)


@router.get(
    "/getallvendorcartypes",
    response_model=Union[List[VendorCarTypeDetail], ErrorResponse],
)
def read_vendor_car_types(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Vendor vehicle model/type catalog for Add Car picker (PR15). JWT active vendor."""
    return get_vendor_car_types_for_vendor(db, user_id)


@router.get(
    "/viewmanagedcarsforvendor",
    response_model=Union[List[VendorManagedCar], ErrorResponse],
)
def read_managed_cars_for_vendor(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Management fleet list (PR15).

    JWT sub is authoritative. Returns pending + approved own cars.
    Soft-deleted excluded. Empty → []. No userAppId query.
    """
    return get_managed_cars_for_vendor(db, user_id)


@router.get(
    "/viewcarsforvendor",
    response_model=Union[List[VendorCarSummaryResponse], NoCarDetailsResponse, ErrorResponse],
)
def read_all_cars_for_vendor(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    userAppId: Optional[str] = Query(None),
):
    """
    Approved cars for JWT vendor (PR11 bidding).

    JWT sub is authoritative. Optional userAppId must match JWT or 403.
    Empty → []. Soft-deleted excluded. Management uses /viewmanagedcarsforvendor.
    """
    return get_vendor_cars_for_bidding(db, user_id=user_id, user_app_id=userAppId)


@router.delete("/deletecarfromprofile", response_model=Union[EmailErrorResponse, ErrorResponse])
def delete_car_legacy(
    delete_data: CarDetailsDelete,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Legacy hard-delete retained for non-Flutter callers. Prefer PUT soft-delete."""
    return delete_car_by_id(db, delete_data)


@router.put(
    "/deletecarfromprofile",
    response_model=Union[EmailErrorResponse, ErrorResponse],
)
def delete_car(
    delete_data: DeleteVendorCarRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Soft-delete own car (PR15). Body: { CARID }. Active-use → 409 CAR_IN_ACTIVE_USE."""
    return delete_car_for_vendor(db, delete_data, user_id)


@router.post("/addcartoprofile", response_model=EmailErrorResponse)
def add_new_car(
    create_data: CreateVendorCarRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Create fleet car (PR15). JWT owner; adminApproved forced false."""
    return insert_car_for_vendor(db, create_data, user_id)

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
