from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional, List, Union, Any
from datetime import date
from enum import Enum
import re

class TrimmedBaseModel(BaseModel):
    """Automatically trims whitespace from all string fields"""
    @field_validator('*', mode='before')
    def strip_whitespace(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v
    
class Gender(str, Enum):
    M = "M"
    F = "F"
    O = "O"
    
class DriverDetailBase(TrimmedBaseModel):
    driverName : str
    driverNumber : str    
    driverGender : str
    driverDOB : date    
    driverCity : str
    driverDocument : str
    driverPhoto : str

    model_config = {"from_attributes":True}

class CreateDriverDetail(TrimmedBaseModel):
    """Create driver body (PR14). Owner derived from JWT — client userAppId ignored."""

    driverName: str
    driverNumber: str
    driverDOB: date
    driverGender: str
    driverCity: str
    driverLicenseImg: str
    driverDocumentImg: str
    driverPhotoImg: str
    driverOtpToken: str
    # Legacy clients may still send userAppId; ignored by CRUD.
    userAppId: Optional[str] = None

    @field_validator('driverGender',mode='after')
    @classmethod
    def normalize_gender(cls,v : str)-> str:
        m = v.strip().upper()
        mapping = {
            "M": "M", "MALE": "M",
            "F": "F", "FEMALE": "F",
            "O": "O", "OTHER": "O", "OTHERS": "O", "NON-BINARY": "O", "NON BINARY": "O"
        }
        if m not in mapping:
            raise ValueError("Invalid gender")
        return mapping[m]
    
    @field_validator('driverNumber',mode='before')
    @classmethod
    def digits_only_and_len(cls,v:str)->str:
        digits = re.sub(r'\D', '', v or "")
        if len(digits) < 10:
            raise ValueError("Invalid driverNumber")
        return digits

    @field_validator('driverOtpToken', mode='before')
    @classmethod
    def require_otp_token(cls, v):
        token = str(v or "").strip()
        if not token:
            raise ValueError("driverOtpToken required")
        return token


class UpdateDriverDetail(TrimmedBaseModel):
    """Update body uses public DRIVERID. DDID accepted as alias for internal mapping."""

    DRIVERID: Optional[int] = None
    DDID: Optional[Any] = None
    driverCity: str
    driverNumber: str
    driverPhotoImg: Optional[str] = None
    driverOtpToken: Optional[str] = None

    @model_validator(mode="after")
    def resolve_driver_id(self):
        if self.DRIVERID is not None:
            return self
        if self.DDID is not None and str(self.DDID).strip() != "":
            try:
                self.DRIVERID = int(str(self.DDID).strip())
            except (TypeError, ValueError):
                raise ValueError("Invalid DRIVERID")
            return self
        raise ValueError("DRIVERID required")

    @field_validator("driverNumber", mode="before")
    @classmethod
    def digits_only_and_len(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v or "")
        if len(digits) < 10:
            raise ValueError("Invalid driverNumber")
        return digits


class DeleteDriverDetail(TrimmedBaseModel):
    driverId: int
    deletedBy: Optional[str] = None
    reason: Optional[str] = None

    @field_validator("driverId", mode="before")
    @classmethod
    def coerce_driver_id(cls, v):
        return int(v)


class DriverDetailResponse(TrimmedBaseModel):
    DRIVERID : int
    USERAPPID : str
    DRIVERNAME : str
    DRIVERNUMBER : str
    DRIVERDOB : str
    GENDER : str
    DRIVERCITY : str
    LICENSE_URL : str
    DOCUMENT_URL : str
    PHOTO_URL : str
    ADDEDON : str

    model_config={"from_attributes":True}


class VendorManagedDriver(TrimmedBaseModel):
    """Management-safe driver row (PR14). Omits USERAPPID, licence/document, FCM."""

    DRIVERID: int
    DRIVERNAME: str
    DRIVERNUMBER: str
    DRIVERDOB: Optional[str] = None
    GENDER: Optional[str] = None
    DRIVERCITY: Optional[str] = None
    PHOTO_URL: Optional[str] = None
    ADDEDON: Optional[str] = None

    model_config = {"from_attributes": True}


class VendorDriverAssignmentSummary(TrimmedBaseModel):
    """Lean driver row for vendor assignment UI (PR13).

    Intentionally omits USERAPPID, DOB/gender/city, licence/document URLs,
    KYC, bank, FCM, and unrelated history fields.
    """

    DRIVERID: int
    DRIVERNAME: str
    PHOTO_URL: Optional[str] = None
    DRIVERNUMBER: Optional[str] = None

    model_config = {"from_attributes": True}
    
class GetAllDriversResponse(TrimmedBaseModel):
    DDID : int
    USERAPPID : Optional[str] = None
    DRIVERNAME : Optional[str] = None
    DRIVERNUMBER : Optional[str] = None
    DRIVERDOB : Optional[str] = None
    DRIVERGENDER : Optional[str] = None
    DRIVERCITY : Optional[str] = None
    DRIVERLICENSE : Optional[str] = None
    DRIVERDOCUMENT : Optional[str] = None
    DRIVERPHOTO : Optional[str] = None
    TABLETIMESTAMP : Optional[str] = None
    VENDORNAME : Optional[str] = None

    model_config={"from_attributes":True}
    
class ErrorResponse(TrimmedBaseModel):
    message : str


class DriverOtpPurpose(str, Enum):
    CREATE_DRIVER = "CREATE_DRIVER"
    CHANGE_DRIVER_PHONE = "CHANGE_DRIVER_PHONE"


class DriverOtpSendRequest(TrimmedBaseModel):
    driverPhone: str
    purpose: DriverOtpPurpose
    driverId: Optional[int] = None

    @field_validator("driverPhone", mode="before")
    @classmethod
    def normalize_phone(cls, v):
        digits = re.sub(r"\D", "", str(v or ""))
        if len(digits) < 10:
            raise ValueError("Invalid driverPhone")
        return digits

    @model_validator(mode="after")
    def require_driver_id_for_change(self):
        if self.purpose == DriverOtpPurpose.CHANGE_DRIVER_PHONE:
            if self.driverId is None:
                raise ValueError("driverId required for CHANGE_DRIVER_PHONE")
        return self


class DriverOtpVerifyRequest(TrimmedBaseModel):
    driverPhone: str
    purpose: DriverOtpPurpose
    otp: str
    driverId: Optional[int] = None

    @field_validator("driverPhone", mode="before")
    @classmethod
    def normalize_phone(cls, v):
        digits = re.sub(r"\D", "", str(v or ""))
        if len(digits) < 10:
            raise ValueError("Invalid driverPhone")
        return digits

    @field_validator("otp", mode="before")
    @classmethod
    def clean_otp(cls, v):
        return str(v or "").strip()

    @model_validator(mode="after")
    def require_driver_id_for_change(self):
        if self.purpose == DriverOtpPurpose.CHANGE_DRIVER_PHONE:
            if self.driverId is None:
                raise ValueError("driverId required for CHANGE_DRIVER_PHONE")
        return self


class DriverOtpVerifyResponse(BaseModel):
    message: str
    driverOtpToken: str


class UploadDriverDocumentRequest(BaseModel):
    driverId: int
    docType: Literal[
        "DRIVERLICENSE",
        "DRIVERDOCUMENT",
        "DRIVERPHOTO",
    ]
    uploadFile: Union[str, List[str]]

    @field_validator("driverId", mode="before")
    @classmethod
    def normalize_driver_id(cls, v):
        if isinstance(v, list):
            return int(v[0]) if v else 0
        return int(v)

    @field_validator("docType", mode="before")
    @classmethod
    def normalize_doc_type(cls, v):
        if isinstance(v, list):
            return str(v[0]).strip().upper() if v else ""
        return str(v).strip().upper()

    @field_validator("uploadFile", mode="before")
    @classmethod
    def normalize_upload_file(cls, v):
        if isinstance(v, list):
            return str(v[0]).strip() if v else ""
        return str(v).strip()

class UploadDriverDocumentResponse(BaseModel):
    status: str
    driverId: int
    docType: str
    column: str
    url: str
    userAppId: str
    driverName: str
    driverNumber: str
