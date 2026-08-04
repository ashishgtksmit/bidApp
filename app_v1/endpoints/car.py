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
from ..auth.deps import AuthenticatedUser, get_current_user

router = APIRouter()

@router.get("/cartypedetails", response_model=List[CarTypeDetailResponse])
def read_car_types(db:Session = Depends(get_db),
                   current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                   ):
    user_id = current_user.user_app_id
    return get_all_car_types(db)


@router.get(
    "/getallvendorcartypes",
    response_model=Union[List[VendorCarTypeDetail], ErrorResponse],
)
def read_vendor_car_types(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Vendor vehicle model/type catalog for Add Car picker (PR15). JWT active vendor."""
    user_id = current_user.user_app_id
    return get_vendor_car_types_for_vendor(db, user_id)


@router.get(
    "/viewmanagedcarsforvendor",
    response_model=Union[List[VendorManagedCar], ErrorResponse],
)
def read_managed_cars_for_vendor(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Management fleet list (PR15).

    JWT sub is authoritative. Returns pending + approved own cars.
    Soft-deleted excluded. Empty → []. No userAppId query.
    """
    user_id = current_user.user_app_id
    return get_managed_cars_for_vendor(db, user_id)


@router.get(
    "/viewcarsforvendor",
    response_model=Union[List[VendorCarSummaryResponse], NoCarDetailsResponse, ErrorResponse],
)
def read_all_cars_for_vendor(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    userAppId: Optional[str] = Query(None),
):
    """
    Approved cars for JWT vendor (PR11 bidding).

    JWT sub is authoritative. Optional userAppId must match JWT or 403.
    Empty → []. Soft-deleted excluded. Management uses /viewmanagedcarsforvendor.
    """
    user_id = current_user.user_app_id
    return get_vendor_cars_for_bidding(db, user_id=user_id, user_app_id=userAppId)


@router.delete("/deletecarfromprofile", response_model=Union[EmailErrorResponse, ErrorResponse])
def delete_car_legacy(
    delete_data: CarDetailsDelete,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Legacy hard-delete retained for non-Flutter callers. Prefer PUT soft-delete."""
    user_id = current_user.user_app_id
    return delete_car_by_id(db, delete_data)


@router.put(
    "/deletecarfromprofile",
    response_model=Union[EmailErrorResponse, ErrorResponse],
)
def delete_car(
    delete_data: DeleteVendorCarRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-delete own car (PR15). Body: { CARID }. Active-use → 409 CAR_IN_ACTIVE_USE."""
    user_id = current_user.user_app_id
    return delete_car_for_vendor(db, delete_data, user_id)


@router.post("/addcartoprofile", response_model=EmailErrorResponse)
def add_new_car(
    create_data: CreateVendorCarRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create fleet car (PR15). JWT owner; adminApproved forced false."""
    user_id = current_user.user_app_id
    return insert_car_for_vendor(db, create_data, user_id)

@router.get("/getallcars", response_model=Union[List[GetAllCarsResponse],NoCarDetailsResponse])
def get_all_cars_endpoint(db:Session = Depends(get_db), 
                             current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                             ):
    user_id = current_user.user_app_id
    return get_all_cars(db)

@router.put("/updatecarapprovalstatus", response_model=ErrorResponse)
def update_car_approval_status_endpoint(update_data: UpdateCarApprovalStatusRequest, 
                                      current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                                      db: Session = Depends(get_db)):
    user_id = current_user.user_app_id
    return update_car_approval_status(db, update_data)


@router.post("/uploadcardocumentbackend",response_model=Union[UploadCarDocumentResponse,ErrorResponse])
def upload_car_document_endpoint(upload_data: UploadCarDocumentRequest,
                                current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                                db: Session = Depends(get_db)):
    user_id = current_user.user_app_id
    return upload_car_document_backend(db, upload_data, user_id)    
