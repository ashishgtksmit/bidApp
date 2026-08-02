from pydantic import BaseModel, field_validator
from typing import Literal, Optional, List, Union
from datetime import datetime
from ..utils.common import UppercaseBase



class TrimmedBaseModel(BaseModel):
    """Automatically trims whitespace from all string fields"""
    @field_validator('*', mode='before')
    def strip_whitespace(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v
    
class CarDetailsBase(TrimmedBaseModel):
    carRegNo : str
    carModel : str
    modelYear : str
    carColor : Optional[str] = None
    ownerName : str
    registrationDoc : str
    powerOfAttorneyDoc : Optional[str] = None
    registeredOn : datetime
    imageVehicleFront : Optional[str] = None
    imageVehicleSide : Optional[str] = None

    @field_validator('carRegNo')
    @classmethod
    def validate_alphanumberic_uppercase(cls,v):
        v = v.replace(' ','').upper()
        if not v or not v.replace('-','').isalnum():
            raise ValueError("Must be alphanumberic with optional hypens")
        return v  

    model_config={"from_attributes":True}

class CarDetailsCreate(CarDetailsBase):
    CARID : int
    userAppId : str
    adminApproved : bool
    carOwnedBySameVendor : bool
    CTD : int

    model_config={"from_attributes":True}

class CarDetailsUpdate(TrimmedBaseModel):
    userAppId : str
    carRegNo : Optional[str] = None
    carModel : Optional[str] = None
    modelYear : Optional[str] = None
    carColor : Optional[str] = None

class CarDetailsDelete(TrimmedBaseModel):
    CARID : int
    deletedBy : str
    reason : str

class CarDetailsResponse(UppercaseBase, CarDetailsCreate):
    CARMODEL : Optional[str]= None
    CARREGNO : Optional[str] = None
    MODELYEAR : Optional[str] = None
    CARCOLOR: Optional[str] = None
    OWNERNAME: Optional[str] = None
    REGISTRATIONDOC: Optional[str] = None
    POWEROFATTORNEYDOC: Optional[str] = None
    REGISTEREDON: Optional[datetime] = None        
    CAR_TYPE: Optional[str] = None
    CAR_SUB_TYPE: Optional[str] = None
    CAPACITY: Optional[int] = None
    IMAGE_URL: Optional[str] = None
    VEHICLE_FRONT: Optional[str] = None
    VEHICLE_SIDE: Optional[str] = None

    model_config = {
        "from_attributes": True,
        "json_encoders": {
            datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
        }
    }


    # Add this at the end of car_details.py

class GetAllCarsResponse(TrimmedBaseModel):
    CARID: int
    USERAPPID: Optional[str] = None
    CARREGNO: Optional[str] = None
    CARMODEL: Optional[str] = None
    MODELYEAR: Optional[str] = None
    CARCOLOR: Optional[str] = None
    OWNERNAME: Optional[str] = None
    REGISTRATIONDOC: Optional[str] = None
    POWEROFFATTORNEYDOC: Optional[str] = None
    REGISTEREDON: Optional[datetime] = None
    ADMINAPPROVED: Optional[bool] = None
    CAROWNEDBYSAMEVENDOR: Optional[bool] = None
    CTD: Optional[int] = None
    IMAGEVEHICLEFRONT: Optional[str] = None
    IMAGEVEHICLESIDE: Optional[str] = None

    model_config = {
        "from_attributes": True,
        "json_encoders": {
            datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
        }
    }
  

class NoCarDetailsResponse(TrimmedBaseModel):
    message : str


class UpdateCarApprovalStatusRequest(TrimmedBaseModel):
    CARID : int
    adminApproved: int

    @field_validator("adminApproved")
    @classmethod
    def validate_status(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError("adminApproved must be 0 or 1")
        return v
    

class UploadCarDocumentRequest(BaseModel):
    carId: int
    docType: Literal[
        "REGISTRATIONDOC",
        "POWEROFFATTORNEYDOC",
        "IMAGEVEHICLEFRONT",
        "IMAGEVEHICLESIDE",
    ]
    uploadFile: Union[str, List[str]]

    @field_validator("carId", mode="before")
    @classmethod
    def normalize_carid(cls, v):
        if isinstance(v, list):
            return int(v[0]) if v else 0
        return int(v)

    @field_validator("docType", mode="before")
    @classmethod
    def normalize_doctype(cls, v):
        if isinstance(v, list):
            return str(v[0]).strip().upper() if v else ""
        return str(v).strip().upper()

    @field_validator("uploadFile", mode="before")
    @classmethod
    def normalize_upload_file(cls, v):
        if isinstance(v, list):
            return str(v[0]).strip() if v else ""
        return str(v).strip()


class UploadCarDocumentResponse(BaseModel):
    status: str
    carId: int
    docType: str
    column: str
    url: str
    userAppId: str
    carRegNo: str


# ---------------------------------------------------------------------------
# PR15 — Vendor Manage Cars (JWT-owned fleet CRUD)
# ---------------------------------------------------------------------------


class VendorManagedCar(TrimmedBaseModel):
    """Management-safe car row. Omits USERAPPID, RC/POA, internal delete fields, FCM."""

    CARID: int
    CARREGNO: str
    CARMODEL: str
    MODELYEAR: Optional[str] = None
    CARCOLOR: Optional[str] = None
    OWNERNAME: Optional[str] = None
    CAR_TYPE: Optional[str] = None
    CAR_SUB_TYPE: Optional[str] = None
    VEHICLE_FRONT: Optional[str] = None
    VEHICLE_SIDE: Optional[str] = None
    ADMINAPPROVED: bool = False
    REGISTEREDON: Optional[str] = None

    model_config = {"from_attributes": True}


class CreateVendorCarRequest(TrimmedBaseModel):
    """Create car body (PR15). Owner / CARID / approval derived server-side.

    Legacy clients may still send userAppId / adminApproved / etc.; ignored by CRUD.
    """

    carRegNo: str
    carModel: str
    CTD: int
    carColor: str
    modelYear: int
    ownerName: str
    imageVehicleRC: str
    imagePowerOfAttorney: Optional[str] = None
    imageVehicleFront: str
    imageVehicleSide: str
    # Ignored / rejected by CRUD if used for ownership or lifecycle:
    userAppId: Optional[str] = None
    CARID: Optional[int] = None
    adminApproved: Optional[bool] = None
    registeredOn: Optional[datetime] = None
    carOwnedBySameVendor: Optional[bool] = None

    @field_validator("CTD", mode="before")
    @classmethod
    def coerce_ctd(cls, v):
        return int(v)

    @field_validator("modelYear", mode="before")
    @classmethod
    def coerce_model_year(cls, v):
        if isinstance(v, bool):
            raise ValueError("modelYear must be a four-digit year")
        if isinstance(v, int):
            return v
        text = str(v or "").strip()
        if not text or not text.isdigit():
            raise ValueError("modelYear must be a four-digit year")
        return int(text)


class DeleteVendorCarRequest(TrimmedBaseModel):
    """PUT /deletecarfromprofile body — CARID only. JWT sub is authoritative."""

    CARID: int

    @field_validator("CARID", mode="before")
    @classmethod
    def coerce_car_id(cls, v):
        return int(v)