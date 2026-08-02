"""PR15 vendor Manage Cars CRUD — JWT ownership, soft-delete, normalized registration.

Does not alter PR11 lean bidding list contract shape.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..crud.vendor_bid import require_active_vendor
from ..models.bid_details import BidDetail
from ..models.car_details import CarDetail
from ..models.car_type_details import CarTypeDetail
from ..models.request_table import Request
from ..models.user_table import User
from ..models.vendor_car_types import VendorCarType
from ..schemas.car_details import (
    CreateVendorCarRequest,
    DeleteVendorCarRequest,
    VendorManagedCar,
)
from ..schemas.vendor_car_types import VendorCarTypeDetail
from ..utils.common import EmailErrorResponse, ErrorResponse
from ..utils.email import send_email
from ..utils.image import azure_blob_delete_by_url, azure_blob_upload

TZ = ZoneInfo("Asia/Kolkata")
MIN_MODEL_YEAR = 1990
ACTIVE_REQUEST_STATUSES = frozenset(
    {"BID - OPEN", "BID - CONFIRMED", "REQUEST - CONFIRMED"}
)
ACTIVE_BID_STATUSES = frozenset(
    {"BID - OPEN", "BID - CONFIRMED", "REQUEST - CONFIRMED"}
)
# Prefer a conservative decoded size for create media (Flutter compresses <1MB).
MAX_CAR_MEDIA_BYTES = 2 * 1024 * 1024


def _clean(v) -> str:
    return str(v or "").strip()


def normalize_car_registration(raw: str) -> str:
    """Trim, uppercase, retain ASCII letters and digits only."""
    text = _clean(raw).upper()
    return re.sub(r"[^A-Z0-9]", "", text)


def normalize_person_name(raw: str) -> str:
    """Trim, collapse whitespace, case-insensitive compare key."""
    text = _clean(raw)
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def _display_registration(raw: str, normalized: str) -> str:
    """Preserve a display-friendly registration; prefer cleaned uppercase input."""
    display = _clean(raw).upper()
    display = re.sub(r"\s+", "", display)
    return display if display else normalized


def _ist_now_naive() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def _format_registered_on(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        text = str(value).strip()
        return text or None


def get_managed_cars_for_vendor(
    db: Session,
    user_id: str,
) -> Union[List[VendorManagedCar], ErrorResponse]:
    """GET /viewmanagedcarsforvendor — pending + approved; soft-deleted excluded."""
    try:
        require_active_vendor(db, user_id)
        vendor_id = str(user_id).strip()

        rows = (
            db.query(
                CarDetail,
                CarTypeDetail.car_type,
                CarTypeDetail.car_sub_type,
            )
            .outerjoin(CarTypeDetail, CarDetail.CTD == CarTypeDetail.CTD)
            .filter(
                CarDetail.userAppId == vendor_id,
                CarDetail.isDeleted == False,  # noqa: E712
            )
            .order_by(CarDetail.registeredOn.desc())
            .all()
        )

        result: List[VendorManagedCar] = []
        for car, car_type, car_sub_type in rows:
            front = car.imageVehicleFront
            if front is not None and str(front).strip() == "":
                front = None
            side = car.imageVehicleSide
            if side is not None and str(side).strip() == "":
                side = None

            result.append(
                VendorManagedCar(
                    CARID=int(car.CARID),
                    CARREGNO=car.carRegNo or "",
                    CARMODEL=car.carModel or "",
                    MODELYEAR=str(car.modelYear) if car.modelYear is not None else None,
                    CARCOLOR=car.carColor,
                    OWNERNAME=car.ownerName,
                    CAR_TYPE=car_type,
                    CAR_SUB_TYPE=car_sub_type,
                    VEHICLE_FRONT=front,
                    VEHICLE_SIDE=side,
                    ADMINAPPROVED=bool(car.adminApproved),
                    REGISTEREDON=_format_registered_on(car.registeredOn),
                )
            )
        return result

    except HTTPException:
        raise
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR")
    except Exception:
        return ErrorResponse(message="ERROR")


def get_vendor_car_types_for_vendor(
    db: Session,
    user_id: str,
) -> Union[List[VendorCarTypeDetail], ErrorResponse]:
    """GET /getallvendorcartypes — JWT active vendor; catalog rows only."""
    try:
        require_active_vendor(db, user_id)

        vendor_car_types = (
            db.query(
                VendorCarType.VCRTID,
                VendorCarType.manufacturer,
                VendorCarType.model,
                VendorCarType.variant,
                VendorCarType.year,
                VendorCarType.fuelType,
                VendorCarType.seatingCapacity,
                VendorCarType.CTD,
                CarTypeDetail.car_type,
                CarTypeDetail.car_sub_type,
                CarTypeDetail.capacity,
                CarTypeDetail.image_url,
            )
            .outerjoin(CarTypeDetail, VendorCarType.CTD == CarTypeDetail.CTD)
            .filter(VendorCarType.CTD.isnot(None))
            .order_by(
                VendorCarType.manufacturer.asc(),
                VendorCarType.model.asc(),
                VendorCarType.year.desc(),
                VendorCarType.variant.asc(),
            )
            .all()
        )

        result: List[VendorCarTypeDetail] = []
        for (
            vcr_tid,
            manufacturer,
            model,
            variant,
            year,
            fuel_type,
            seating_capacity,
            ctd,
            car_type,
            car_sub_type,
            capacity,
            image_url,
        ) in vendor_car_types:
            if ctd is None:
                continue
            # Skip orphan CTD with no matching car type catalog when join fails
            # but CTD was set — still return if CTD is present on vendor row.
            result.append(
                VendorCarTypeDetail(
                    vcrtid=int(vcr_tid),
                    manufacturer=manufacturer or "",
                    model=model or "",
                    variant=variant or "",
                    year=str(year) if year is not None else "",
                    fuelType=fuel_type or "",
                    seatingCapacity=int(seating_capacity or 0),
                    CTD=int(ctd),
                    car_type=car_type,
                    car_Sub_Type=car_sub_type,
                    capacity=str(capacity) if capacity is not None else None,
                    image_Url=image_url,
                )
            )
        return result

    except HTTPException:
        raise
    except SQLAlchemyError:
        return ErrorResponse(message="ERROR")
    except Exception:
        return ErrorResponse(message="ERROR")


def _validate_ctd_exists(db: Session, ctd: int) -> None:
    row = (
        db.query(VendorCarType.VCRTID, CarTypeDetail.CTD)
        .outerjoin(CarTypeDetail, VendorCarType.CTD == CarTypeDetail.CTD)
        .filter(VendorCarType.CTD == ctd)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_CTD",
        )


def _car_in_active_use(db: Session, car_id: int) -> bool:
    """True when CARID is referenced by an active bid/request relationship."""
    bid_hit = (
        db.query(BidDetail.BID)
        .filter(
            BidDetail.CARID == car_id,
            BidDetail.bidStatus.in_(ACTIVE_BID_STATUSES),
        )
        .with_for_update()
        .first()
    )
    if bid_hit is not None:
        return True

    # Confirmed/open request whose selected winning bid uses this car
    req_hit = (
        db.query(BidDetail.BID)
        .join(Request, Request.RID == BidDetail.rID)
        .filter(
            BidDetail.CARID == car_id,
            Request.requestStatus.in_(ACTIVE_REQUEST_STATUSES),
            BidDetail.bidStatus.in_(ACTIVE_BID_STATUSES),
        )
        .with_for_update()
        .first()
    )
    return req_hit is not None


def insert_car_for_vendor(
    db: Session,
    create_data: CreateVendorCarRequest,
    user_id: str,
) -> Union[EmailErrorResponse, ErrorResponse]:
    """POST /addcartoprofile — JWT owner; adminApproved=false; global reg unique."""
    EMAIL_SUBJECT = "OpenBid | New Vehicle Added"
    EMAIL_FROM = "ticketdetails@wizzride.com"
    EMAIL_FROM_NAME = "WizzRide"
    EMAIL_TO = "openbidresourceteam@wizzride.com"
    EMAIL_TO_NAME = "OpenBid Resource Team"

    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()

    vendor = db.query(User).filter(User.userAppId == vendor_id).first()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized as an active vendor",
        )

    car_reg_raw = _clean(create_data.carRegNo)
    normalized = normalize_car_registration(car_reg_raw)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_CARREGNO",
        )
    display_reg = _display_registration(car_reg_raw, normalized)

    car_model = _clean(create_data.carModel)
    if not car_model:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_CARMODEL",
        )

    car_color = _clean(create_data.carColor).upper()
    if not car_color:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_CARCOLOR",
        )

    owner_name = _clean(create_data.ownerName)
    if not owner_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_OWNERNAME",
        )

    try:
        model_year = int(create_data.modelYear)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_MODELYEAR",
        )

    current_year = datetime.now(TZ).year
    if model_year < MIN_MODEL_YEAR or model_year > current_year:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_MODELYEAR",
        )

    try:
        ctd = int(create_data.CTD)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_CTD",
        )
    _validate_ctd_exists(db, ctd)

    rc = _clean(create_data.imageVehicleRC)
    front = _clean(create_data.imageVehicleFront)
    side = _clean(create_data.imageVehicleSide)
    if not rc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_IMAGEVEHICLERC",
        )
    if not front:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_IMAGEVEHICLEFRONT",
        )
    if not side:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_IMAGEVEHICLESIDE",
        )

    vendor_full_name = _clean(getattr(vendor, "fullName", None))
    same_owner = normalize_person_name(owner_name) == normalize_person_name(
        vendor_full_name
    )
    poa = _clean(create_data.imagePowerOfAttorney)
    if not same_owner and not poa:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_MISSING_IMAGEPOWEROFATTORNEY",
        )
    if same_owner:
        poa = ""

    # Duplicate check before blob upload
    existing = (
        db.query(CarDetail.CARID)
        .filter(CarDetail.normalizedCarRegNo == normalized)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="CAR_ALREADY_EXISTS",
        )

    registered_on = _ist_now_naive()
    registered_on_str = registered_on.strftime("%Y-%m-%d %H:%M:%S")
    new_blob_urls: list[str] = []
    # Safe server-generated blob stem (no client paths)
    base_blob = f"{vendor_id}/{normalized}/"

    try:
        ok_rc, url_rc = azure_blob_upload(
            blob_name=f"{base_blob}VehicleRC",
            base64_data=rc,
            make_public=False,
            max_upload_bytes=MAX_CAR_MEDIA_BYTES,
        )
        if not ok_rc:
            detail = _map_media_error(url_rc, "RC")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            )
        new_blob_urls.append(url_rc)

        url_poa = None
        if poa:
            ok_poa, url_poa = azure_blob_upload(
                blob_name=f"{base_blob}PowerOfAttorney",
                base64_data=poa,
                make_public=False,
                max_upload_bytes=MAX_CAR_MEDIA_BYTES,
            )
            if not ok_poa:
                detail = _map_media_error(url_poa, "POA")
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=detail,
                )
            new_blob_urls.append(url_poa)

        ok_front, url_front = azure_blob_upload(
            blob_name=f"{base_blob}VehicleFront",
            base64_data=front,
            make_public=True,
            max_upload_bytes=MAX_CAR_MEDIA_BYTES,
        )
        if not ok_front:
            detail = _map_media_error(url_front, "FRONT")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            )
        new_blob_urls.append(url_front)

        ok_side, url_side = azure_blob_upload(
            blob_name=f"{base_blob}VehicleSide",
            base64_data=side,
            make_public=True,
            max_upload_bytes=MAX_CAR_MEDIA_BYTES,
        )
        if not ok_side:
            detail = _map_media_error(url_side, "SIDE")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            )
        new_blob_urls.append(url_side)

        new_car = CarDetail(
            userAppId=vendor_id,
            carRegNo=display_reg,
            normalizedCarRegNo=normalized,
            carModel=car_model,
            modelYear=str(model_year),
            carColor=car_color,
            ownerName=owner_name,
            CTD=ctd,
            registrationDoc=url_rc,
            powerOfAttorneyDoc=url_poa,
            imageVehicleFront=url_front,
            imageVehicleSide=url_side,
            carOwnedBySameVendor=same_owner,
            adminApproved=False,
            isDeleted=False,
            deletedAt=None,
            deletedBy=None,
            registeredOn=registered_on,
        )
        db.add(new_car)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _cleanup_blobs(new_blob_urls)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="CAR_ALREADY_EXISTS",
            )

        # Best-effort operational email after commit
        html = f"""
        <html>
        <head>
            <title>New Vehicle Added - OpenBid</title>
            <style>
                body {{ font-family: Arial, sans-serif; color: #333; background: #f5f7fb; }}
                .wrap {{ max-width: 700px; margin: 24px auto; }}
                .box  {{ padding: 18px 20px; border: 1px solid #e6e9f0; border-radius: 12px; background: #fff; }}
                h2    {{ margin: 0 0 12px; color: #1f2d3d; }}
                .grid {{ display: grid; grid-template-columns: 240px 1fr; gap: 8px 14px; }}
                .k    {{ font-weight: 600; color: #2c3e50; }}
                .sep  {{ height: 1px; background: #eceff4; margin: 14px 0; }}
                a     {{ color: #0b5ed7; text-decoration: none; }}
                .muted{{ color: #6b7280; }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <div class="box">
                    <h2>New Vehicle Added</h2>
                    <div class="grid">
                        <div class="k">User App ID</div><div>{vendor_id}</div>
                        <div class="k">Car Number</div><div>{display_reg}</div>
                        <div class="k">Car Model</div><div>{car_model}</div>
                        <div class="k">Model Year</div><div>{model_year}</div>
                        <div class="k">Car Color</div><div>{car_color}</div>
                        <div class="k">Owner Name</div><div>{owner_name}</div>
                        <div class="k">CTD</div><div>{ctd}</div>
                    </div>
                    <div class="sep"></div>
                    <div class="grid">
                        <div class="k">Vehicle RC</div><div><a href="{url_rc}" target="_blank">{url_rc}</a></div>
                        <div class="k">Power of Attorney</div><div>{f'<a href="{url_poa}" target="_blank">{url_poa}</a>' if url_poa else '<span class="muted">Not provided</span>'}</div>
                        <div class="k">Vehicle Front</div><div><a href="{url_front}" target="_blank">{url_front}</a></div>
                        <div class="k">Vehicle Side</div><div><a href="{url_side}" target="_blank">{url_side}</a></div>
                    </div>
                    <div class="sep"></div>
                    <div class="grid">
                        <div class="k">Car Owned by Same Vendor</div><div>{"Yes" if same_owner else "No"}</div>
                        <div class="k">Admin Approved</div><div>Pending</div>
                        <div class="k">Registered On (IST)</div><div>{registered_on_str}</div>
                    </div>
                    <div class="sep"></div>
                    <div class="muted">This is an automated message. Please do not reply.</div>
                </div>
            </div>
        </body>
        </html>
        """
        try:
            send_email(
                message=html,
                subject=EMAIL_SUBJECT,
                from_address=EMAIL_FROM,
                from_name=EMAIL_FROM_NAME,
                to_address=EMAIL_TO,
                to_name=EMAIL_TO_NAME,
            )
        except Exception:
            pass

        return EmailErrorResponse(message="INSERTED")

    except HTTPException:
        _cleanup_blobs(new_blob_urls)
        raise
    except SQLAlchemyError:
        db.rollback()
        _cleanup_blobs(new_blob_urls)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ERROR",
        )
    except Exception:
        db.rollback()
        _cleanup_blobs(new_blob_urls)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ERROR",
        )


def delete_car_for_vendor(
    db: Session,
    delete_data: DeleteVendorCarRequest,
    user_id: str,
) -> Union[EmailErrorResponse, ErrorResponse]:
    """PUT /deletecarfromprofile — soft-delete with active-use gates."""
    require_active_vendor(db, user_id)
    vendor_id = str(user_id).strip()

    try:
        car_id = int(delete_data.CARID)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERROR_INVALID_CARID",
        )

    try:
        car = (
            db.query(CarDetail)
            .filter(CarDetail.CARID == car_id)
            .with_for_update()
            .first()
        )
        if car is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )
        if bool(getattr(car, "isDeleted", False)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NOT_FOUND",
            )
        if str(car.userAppId).strip() != vendor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this car",
            )

        if _car_in_active_use(db, car_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="CAR_IN_ACTIVE_USE",
            )

        now = _ist_now_naive()
        car.isDeleted = True
        car.deletedAt = now
        car.deletedBy = vendor_id
        db.commit()
        return ErrorResponse(message="DELETED")

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ERROR",
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ERROR",
        )


def _map_media_error(upload_message: Optional[str], label: str) -> str:
    msg = str(upload_message or "").upper()
    if "FILE_TOO_LARGE" in msg:
        return "ERROR_MEDIA_TOO_LARGE"
    if "INVALID_BASE64" in msg:
        return "ERROR_INVALID_MEDIA"
    if "INVALID_IMAGE" in msg or "UNSUPPORTED_IMAGE" in msg:
        return "ERROR_INVALID_MEDIA"
    return f"ERROR_SAVE_{label}"


def _cleanup_blobs(urls: list[str]) -> None:
    for url in urls:
        try:
            azure_blob_delete_by_url(url)
        except Exception:
            pass


def car_is_bid_eligible(car: CarDetail, vendor_id: str) -> Optional[str]:
    """Return HTTP detail reason if car cannot be used for bidding, else None."""
    if car is None:
        return "Car not found"
    if bool(getattr(car, "isDeleted", False)):
        return "Car is not eligible"
    if str(car.userAppId).strip() != str(vendor_id).strip():
        return "Not authorized to bid with this car"
    if not bool(car.adminApproved):
        return "Car is not eligible"
    return None
