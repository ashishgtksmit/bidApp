from fastapi import APIRouter, Depends, Query, Request, HTTPException, status
from sqlalchemy.orm import Session
from typing import Union, List, Optional, Any

from ..utils.common import EmailErrorResponse, ErrorResponse
from ..schemas.driver_details import (
    UpdateDriverDetail,
    DeleteDriverDetail,
    CreateDriverDetail,
    GetAllDriversResponse,
    UploadDriverDocumentRequest,
    UploadDriverDocumentResponse,
    VendorDriverAssignmentSummary,
    VendorManagedDriver,
    DriverOtpSendRequest,
    DriverOtpVerifyRequest,
    DriverOtpVerifyResponse,
    DriverOtpPurpose,
)
from ..database import get_db
from ..crud.driver import (
    get_all_driver_for_vendor,
    get_all_drivers,
    upload_driver_document_backend,
)
from ..crud.driver_manage import (
    get_managed_drivers_for_vendor,
    insert_driver_for_vendor,
    update_driver_for_vendor,
    delete_driver_for_vendor,
)
from ..crud.vendor_bid import require_active_vendor
from ..auth.deps import get_current_user_id
from ..models.driver_details import DriverDetail
from ..utils.driver_otp import (
    PURPOSE_CHANGE_DRIVER_PHONE,
    PURPOSE_CREATE_DRIVER,
    send_driver_otp,
    verify_driver_otp,
)
from ..utils.rate_limit import enforce_rate_limit, client_ip_from_request
import os

router = APIRouter()


def _raise_from_error_response(result: ErrorResponse) -> None:
    msg = (result.message or "").upper()
    if msg == "RATE_LIMITED" or msg == "ERROR_TOO_MANY_ATTEMPTS":
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=result.message,
        )
    if msg == "ERROR_OTP_EXPIRED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.message,
        )
    if msg in ("ERROR_INVALID_OTP", "ERROR_INVALID_PHONE", "ERROR_INVALID_PURPOSE"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result.message,
        )
    if msg == "ERROR_SENDING_SMS":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.message,
        )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="ERROR",
    )


@router.post(
    "/updatedriverdetails",
    response_model=Union[EmailErrorResponse, ErrorResponse],
)
def driver_details_update(
    driver_data: UpdateDriverDetail,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return update_driver_for_vendor(db, driver_data, user_id)


@router.put(
    "/deletedriverfromprofile",
    response_model=Union[EmailErrorResponse, ErrorResponse],
)
def delete_driver(
    driver_data: DeleteDriverDetail,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return delete_driver_for_vendor(db, driver_data, user_id)


@router.post("/insertnewdriver", response_model=Union[EmailErrorResponse, ErrorResponse])
def create_new_driver(
    driver_data: CreateDriverDetail,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return insert_driver_for_vendor(db, driver_data, user_id)


@router.get(
    "/viewdriversforvendor",
    response_model=Union[List[VendorDriverAssignmentSummary], ErrorResponse],
)
def read_all_drivers_for_vendors(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    userAppId: Optional[str] = Query(None),
):
    """
    Lean vendor-owned driver list for assignment UI (PR13).

    JWT sub is authoritative. Optional userAppId must equal JWT sub when supplied.
    Empty ownership → []. Does not apply admin/KYC/availability filters.
    """
    return get_all_driver_for_vendor(
        db,
        user_id=user_id,
        userappid=userAppId,
    )


@router.get(
    "/viewmanageddriversforvendor",
    response_model=Union[List[VendorManagedDriver], ErrorResponse],
)
def read_managed_drivers_for_vendor(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Management-safe driver list for Manage Drivers UI (PR14).

    JWT sub is authoritative. Soft-deleted excluded via ownership.
    Omits USERAPPID, LICENSE_URL, DOCUMENT_URL, FCM.
    """
    return get_managed_drivers_for_vendor(db, user_id=user_id)


@router.post("/driverotp/send", response_model=ErrorResponse)
def driver_otp_send(
    body: DriverOtpSendRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()

    purpose = body.purpose.value
    driver_id = body.driverId

    if purpose == PURPOSE_CHANGE_DRIVER_PHONE:
        driver = (
            db.query(DriverDetail)
            .filter(DriverDetail.DDID == int(driver_id))
            .first()
        )
        if driver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )
        if str(driver.userAppId) != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this driver",
            )

    ip = client_ip_from_request(request)
    ip_limit = int(os.getenv("RATE_LIMIT_DRIVER_OTP_SEND_IP", "20"))
    user_limit = int(os.getenv("RATE_LIMIT_DRIVER_OTP_SEND_USER", "5"))
    window = int(os.getenv("RATE_LIMIT_DRIVER_OTP_WINDOW_SECONDS", "900"))

    limited = enforce_rate_limit(
        db,
        bucket_key=f"driverotp_send:ip:{ip}",
        max_hits=ip_limit,
        window_seconds=window,
    )
    if limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="RATE_LIMITED",
        )
    limited = enforce_rate_limit(
        db,
        bucket_key=f"driverotp_send:vendor:{vendor_id}",
        max_hits=user_limit,
        window_seconds=window,
    )
    if limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="RATE_LIMITED",
        )

    result = send_driver_otp(
        db,
        vendor_app_id=vendor_id,
        driver_phone=body.driverPhone,
        purpose=purpose,
        driver_id=driver_id if purpose == PURPOSE_CHANGE_DRIVER_PHONE else None,
    )
    if result.message != "OTP_SENT":
        _raise_from_error_response(result)
    return result


@router.post("/driverotp/verify", response_model=DriverOtpVerifyResponse)
def driver_otp_verify(
    body: DriverOtpVerifyRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()
    purpose = body.purpose.value
    driver_id = body.driverId

    if purpose == PURPOSE_CHANGE_DRIVER_PHONE:
        driver = (
            db.query(DriverDetail)
            .filter(DriverDetail.DDID == int(driver_id))
            .first()
        )
        if driver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )
        if str(driver.userAppId) != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this driver",
            )

    ip = client_ip_from_request(request)
    ip_limit = int(os.getenv("RATE_LIMIT_DRIVER_OTP_VERIFY_IP", "60"))
    user_limit = int(os.getenv("RATE_LIMIT_DRIVER_OTP_VERIFY_USER", "20"))
    window = int(os.getenv("RATE_LIMIT_DRIVER_OTP_WINDOW_SECONDS", "900"))

    limited = enforce_rate_limit(
        db,
        bucket_key=f"driverotp_verify:ip:{ip}",
        max_hits=ip_limit,
        window_seconds=window,
    )
    if limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="RATE_LIMITED",
        )
    limited = enforce_rate_limit(
        db,
        bucket_key=f"driverotp_verify:vendor:{vendor_id}",
        max_hits=user_limit,
        window_seconds=window,
    )
    if limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="RATE_LIMITED",
        )

    result = verify_driver_otp(
        db,
        vendor_app_id=vendor_id,
        driver_phone=body.driverPhone,
        purpose=purpose,
        otp=body.otp,
        driver_id=driver_id if purpose == PURPOSE_CHANGE_DRIVER_PHONE else None,
    )
    if isinstance(result, ErrorResponse):
        _raise_from_error_response(result)
    return DriverOtpVerifyResponse(
        message=result["message"],
        driverOtpToken=result["driverOtpToken"],
    )


@router.get("/getalldrivers", response_model=Union[GetAllDriversResponse, ErrorResponse])
def get_all_drivers_endpoint(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return get_all_drivers(db)


@router.post(
    "/uploaddriverdocumentbackend",
    response_model=Union[UploadDriverDocumentResponse, ErrorResponse],
)
def upload_driver_document_endpoint(
    request: UploadDriverDocumentRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return upload_driver_document_backend(db, request)
