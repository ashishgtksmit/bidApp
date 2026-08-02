"""PR14 vendor Manage Drivers CRUD — JWT ownership, OTP tokens, management list.

Does not alter PR13 lean assignment list contract.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..crud.vendor_bid import require_active_vendor
from ..models.driver_details import DriverDetail
from ..models.request_table import Request
from ..schemas.driver_details import (
    CreateDriverDetail,
    DeleteDriverDetail,
    UpdateDriverDetail,
    VendorManagedDriver,
)
from ..utils.common import EmailErrorResponse, ErrorResponse
from ..utils.driver_otp import (
    PURPOSE_CHANGE_DRIVER_PHONE,
    PURPOSE_CREATE_DRIVER,
    consume_driver_otp_token,
    normalize_driver_phone,
    validate_driver_otp_token,
    validate_driver_phone,
)
from ..utils.email import send_email
from ..utils.image import azure_blob_delete_by_url, azure_blob_upload

SOFT_DELETE_SENTINEL = "123456789"
ACTIVE_TRIP_STATUS = "REQUEST - CONFIRMED"
TZ = ZoneInfo("Asia/Kolkata")


def _clean(v) -> str:
    return str(v or "").strip()


def _normalize_gender(raw: str) -> Optional[str]:
    g = _clean(raw).upper()
    if g in ("M", "MALE"):
        return "M"
    if g in ("F", "FEMALE"):
        return "F"
    if g in ("O", "OTHER", "OTHERS", "NON-BINARY", "NON BINARY"):
        return "O"
    return None


def _parse_dob(raw) -> Optional[date]:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    dob_input = _clean(raw)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(dob_input, fmt).date()
        except ValueError:
            continue
    return None


def _dob_not_future(dob_value: date) -> bool:
    return dob_value <= datetime.now(TZ).date()


def get_managed_drivers_for_vendor(
    db: Session,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> Union[List[VendorManagedDriver], ErrorResponse]:
    """Management list — own active drivers only; soft-deleted excluded; newest first."""
    try:
        require_active_vendor(db, user_id)
        vendor_id = str(user_id).strip()

        try:
            limit = int(limit)
        except Exception:
            limit = 50
        try:
            offset = int(offset)
        except Exception:
            offset = 0
        if limit <= 0 or limit > 500:
            limit = 50
        if offset < 0:
            offset = 0

        rows = (
            db.query(DriverDetail)
            .filter(DriverDetail.userAppId == vendor_id)
            .order_by(DriverDetail.tableTimestamp.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

        result: List[VendorManagedDriver] = []
        for row in rows:
            added_on = None
            if row.tableTimestamp is not None:
                try:
                    added_on = row.tableTimestamp.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    added_on = str(row.tableTimestamp)

            dob_str = None
            if row.driverDOB is not None:
                dob_str = (
                    row.driverDOB.isoformat()
                    if hasattr(row.driverDOB, "isoformat")
                    else str(row.driverDOB)
                )

            photo = row.driverPhoto
            if photo is not None and str(photo).strip() == "":
                photo = None

            result.append(
                VendorManagedDriver(
                    DRIVERID=int(row.DDID),
                    DRIVERNAME=row.driverName or "",
                    DRIVERNUMBER=row.driverNumber or "",
                    DRIVERDOB=dob_str,
                    GENDER=row.driverGender,
                    DRIVERCITY=row.driverCity,
                    PHOTO_URL=photo,
                    ADDEDON=added_on,
                )
            )
        return result

    except HTTPException:
        raise
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR")
    except Exception:
        return ErrorResponse(message="ERROR")


def insert_driver_for_vendor(
    db: Session,
    driver_data: CreateDriverDetail,
    user_id: str,
) -> Union[EmailErrorResponse, ErrorResponse]:
    """Create driver owned by JWT vendor. Requires CREATE_DRIVER OTP token."""
    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()

    driver_name = _clean(driver_data.driverName)
    if not driver_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_DRIVERNAME",
        )

    phone = validate_driver_phone(driver_data.driverNumber)
    if phone is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_DRIVERNUMBER",
        )

    driver_gender = _normalize_gender(driver_data.driverGender)
    if driver_gender is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_GENDER",
        )

    driver_dob = _parse_dob(driver_data.driverDOB)
    if driver_dob is None or not _dob_not_future(driver_dob):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_DOB",
        )

    driver_city = _clean(driver_data.driverCity)
    if not driver_city:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_DRIVERCITY",
        )

    for field_name, value in (
        ("driverLicenseImg", driver_data.driverLicenseImg),
        ("driverDocumentImg", driver_data.driverDocumentImg),
        ("driverPhotoImg", driver_data.driverPhotoImg),
    ):
        if not _clean(value):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"ERROR_MISSING_{field_name.upper()}",
            )

    token_row = validate_driver_otp_token(
        db,
        raw_token=driver_data.driverOtpToken,
        vendor_app_id=vendor_id,
        driver_phone=phone,
        purpose=PURPOSE_CREATE_DRIVER,
        driver_id=None,
    )
    if isinstance(token_row, ErrorResponse):
        msg = token_row.message
        if msg == "ERROR_OTP_TOKEN_REQUIRED":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=msg,
            )
        if msg == "ERROR_OTP_TOKEN_EXPIRED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=msg,
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_OTP_TOKEN",
        )

    safe_user = re.sub(r"[^A-Za-z0-9_\-]", "", vendor_id)
    driver_folder = re.sub(r"[^A-Za-z0-9_\- ]", "", driver_name)
    driver_folder = re.sub(r"\s+", "_", driver_folder.strip())
    if driver_folder == "":
        driver_folder = "Driver"
    base_blob = f"{safe_user}/drivers/{driver_folder}/"
    table_timestamp = datetime.now(TZ).replace(tzinfo=None)

    new_blob_urls: List[str] = []

    try:
        existing = (
            db.query(DriverDetail)
            .filter(
                DriverDetail.userAppId == vendor_id,
                DriverDetail.driverNumber == phone,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ERROR_ALREADY_EXISTS",
            )

        ok_lic, driver_license_url = azure_blob_upload(
            blob_name=f"{base_blob}DriverLicense_{phone}",
            base64_data=driver_data.driverLicenseImg,
            make_public=True,
        )
        if not ok_lic:
            detail = _map_media_error(driver_license_url, "LICENSE")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            )
        new_blob_urls.append(driver_license_url)

        ok_doc, driver_document_url = azure_blob_upload(
            blob_name=f"{base_blob}DriverDocument_{phone}",
            base64_data=driver_data.driverDocumentImg,
            make_public=False,
        )
        if not ok_doc:
            detail = _map_media_error(driver_document_url, "DOCUMENT")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            )
        new_blob_urls.append(driver_document_url)

        ok_photo, driver_photo_url = azure_blob_upload(
            blob_name=f"{base_blob}DriverPhoto_{phone}",
            base64_data=driver_data.driverPhotoImg,
            make_public=True,
        )
        if not ok_photo:
            detail = _map_media_error(driver_photo_url, "PHOTO")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            )
        new_blob_urls.append(driver_photo_url)

        new_driver = DriverDetail(
            userAppId=vendor_id,
            driverName=driver_name,
            driverNumber=phone,
            driverDOB=driver_dob,
            driverGender=driver_gender,
            driverCity=driver_city,
            driverLicense=driver_license_url,
            driverDocument=driver_document_url,
            driverPhoto=driver_photo_url,
            tableTimestamp=table_timestamp,
        )
        db.add(new_driver)
        consume_driver_otp_token(db, token_row)
        db.commit()

        try:
            _send_insert_email(
                vendor_id=vendor_id,
                driver_name=driver_name,
                driver_number=phone,
                driver_dob=driver_dob.isoformat(),
                driver_gender=driver_gender,
                driver_city=driver_city,
                license_url=driver_license_url,
                document_url=driver_document_url,
                photo_url=driver_photo_url,
                table_timestamp=table_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            pass

        return EmailErrorResponse(message="INSERTED")

    except HTTPException:
        db.rollback()
        _cleanup_blobs(new_blob_urls)
        raise
    except SQLAlchemyError:
        db.rollback()
        _cleanup_blobs(new_blob_urls)
        return ErrorResponse(message="ERROR")
    except Exception:
        db.rollback()
        _cleanup_blobs(new_blob_urls)
        return ErrorResponse(message="ERROR")


def update_driver_for_vendor(
    db: Session,
    driver_data: UpdateDriverDetail,
    user_id: str,
) -> Union[EmailErrorResponse, ErrorResponse]:
    """Update city / phone / optional photo. Phone change requires CHANGE_DRIVER_PHONE token."""
    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()

    driver_id = driver_data.DRIVERID
    if driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_DRIVERID",
        )

    driver_city = _clean(driver_data.driverCity)
    phone = validate_driver_phone(driver_data.driverNumber)
    if not driver_city or phone is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_FIELDS",
        )

    new_photo_url = None
    token_row = None

    try:
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
                detail="Not authorized to update this driver",
            )

        old_phone = normalize_driver_phone(driver.driverNumber)
        phone_changed = old_phone != phone
        old_city = _clean(driver.driverCity)
        old_photo_url = driver.driverPhoto or None
        photo_input = getattr(driver_data, "driverPhotoImg", None)
        has_new_photo = bool(photo_input and _clean(photo_input))

        if phone_changed:
            token_result = validate_driver_otp_token(
                db,
                raw_token=driver_data.driverOtpToken or "",
                vendor_app_id=vendor_id,
                driver_phone=phone,
                purpose=PURPOSE_CHANGE_DRIVER_PHONE,
                driver_id=int(driver_id),
            )
            if isinstance(token_result, ErrorResponse):
                msg = token_result.message
                if msg == "ERROR_OTP_TOKEN_REQUIRED":
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="ERROR_OTP_TOKEN_REQUIRED",
                    )
                if msg == "ERROR_OTP_TOKEN_EXPIRED":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=msg,
                    )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="ERROR_INVALID_OTP_TOKEN",
                )
            token_row = token_result

            dup = (
                db.query(DriverDetail)
                .filter(
                    DriverDetail.userAppId == vendor_id,
                    DriverDetail.driverNumber == phone,
                    DriverDetail.DDID != int(driver_id),
                )
                .first()
            )
            if dup:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="ERROR_ALREADY_EXISTS",
                )

        # Same-value no-op (city + phone unchanged, no photo)
        if (
            not phone_changed
            and old_city == driver_city
            and not has_new_photo
        ):
            return ErrorResponse(message="UPDATED")

        safe_user = re.sub(r"[^A-Za-z0-9_\-]", "", vendor_id)
        driver_folder = re.sub(r"[^A-Za-z0-9_\- ]", "", driver.driverName or "")
        driver_folder = re.sub(r"\s+", " ", driver_folder).strip()
        if driver_folder == "":
            driver_folder = "Driver"
        base_blob = f"{safe_user}/drivers/{driver_folder}/"
        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")

        if has_new_photo:
            ok, uploaded_url = azure_blob_upload(
                blob_name=f"{base_blob}DriverPhoto_{ts}",
                base64_data=photo_input,
                make_public=True,
            )
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=_map_media_error(uploaded_url, "PHOTO"),
                )
            new_photo_url = uploaded_url

        changed = phone_changed or old_city != driver_city or has_new_photo
        if changed:
            driver.driverCity = driver_city
            driver.driverNumber = phone
            if new_photo_url:
                driver.driverPhoto = new_photo_url
            driver.tableTimestamp = datetime.now(TZ).replace(tzinfo=None)

        if token_row is not None:
            consume_driver_otp_token(db, token_row)

        db.commit()

        if new_photo_url and old_photo_url:
            try:
                azure_blob_delete_by_url(old_photo_url)
            except Exception:
                pass

        return ErrorResponse(message="UPDATED")

    except HTTPException:
        db.rollback()
        if new_photo_url:
            _cleanup_blobs([new_photo_url])
        raise
    except SQLAlchemyError:
        db.rollback()
        if new_photo_url:
            _cleanup_blobs([new_photo_url])
        return ErrorResponse(message="ERROR")
    except Exception:
        db.rollback()
        if new_photo_url:
            _cleanup_blobs([new_photo_url])
        return ErrorResponse(message="ERROR")


def delete_driver_for_vendor(
    db: Session,
    driver_data: DeleteDriverDetail,
    user_id: str,
) -> Union[EmailErrorResponse, ErrorResponse]:
    """Soft-delete own driver. Blocks when assigned to active confirmed trip."""
    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()

    try:
        driver_id = int(driver_data.driverId)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_DRIVERID",
        )

    try:
        driver = (
            db.query(DriverDetail)
            .filter(DriverDetail.DDID == driver_id)
            .first()
        )
        if driver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )

        owner = str(driver.userAppId or "")
        if owner == SOFT_DELETE_SENTINEL:
            # Already soft-deleted — do not reveal former ownership
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )

        if owner != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this driver",
            )

        active_assignment = (
            db.query(Request.RID)
            .filter(
                Request.driverAssignedID == driver_id,
                Request.requestStatus == ACTIVE_TRIP_STATUS,
            )
            .first()
        )
        if active_assignment is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="DRIVER_ASSIGNED_TO_ACTIVE_TRIP",
            )

        driver.userAppId = SOFT_DELETE_SENTINEL
        driver.tableTimestamp = datetime.now(TZ).replace(tzinfo=None)
        db.commit()

        return ErrorResponse(message="DELETED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        return ErrorResponse(message="ERROR")
    except Exception:
        db.rollback()
        return ErrorResponse(message="ERROR")


def _map_media_error(upload_code: str, kind: str) -> str:
    code = str(upload_code or "").upper()
    if "FILE_TOO_LARGE" in code:
        return "ERROR_MEDIA_TOO_LARGE"
    if "INVALID_BASE64" in code or "INVALID_MIME" in code or "UNSUPPORTED" in code:
        return f"ERROR_INVALID_{kind}_IMAGE"
    if "INVALID_IMAGE" in code or "INVALID_FILE" in code:
        return f"ERROR_INVALID_{kind}_IMAGE"
    return f"ERROR_SAVE_{kind}"


def _cleanup_blobs(urls: List[str]) -> None:
    for url in urls:
        try:
            azure_blob_delete_by_url(url)
        except Exception:
            pass


def _send_insert_email(
    *,
    vendor_id: str,
    driver_name: str,
    driver_number: str,
    driver_dob: str,
    driver_gender: str,
    driver_city: str,
    license_url: str,
    document_url: str,
    photo_url: str,
    table_timestamp: str,
) -> None:
    html = f'''
    <html><body>
    <h2>New Driver Added</h2>
    <div>User App ID: {vendor_id}</div>
    <div>Driver Name: {driver_name}</div>
    <div>Driver Number: {driver_number}</div>
    <div>DOB: {driver_dob}</div>
    <div>Gender: {driver_gender}</div>
    <div>City: {driver_city}</div>
    <div>License: {license_url}</div>
    <div>Document: {document_url}</div>
    <div>Photo: {photo_url}</div>
    <div>Added On: {table_timestamp}</div>
    </body></html>
    '''
    send_email(
        message=html,
        subject="OpenBid | New Driver Added",
        from_address="ticketdetails@wizzride.com",
        from_name="WizzRide",
        to_address="openbidresourceteam@wizzride.com",
        to_name="OpenBid Resource Team",
    )
