from pydantic import BaseModel, field_validator, constr
from typing import Literal, Optional, List, Union
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
    userAppId : str    
    driverName: str
    driverNumber: str
    driverDOB: date                      # let Pydantic parse "YYYY-MM-DD"
    driverGender: str                    # we’ll normalize to Gender in validator
    driverCity: str
    # base64 images expected for upload
    driverLicenseImg: str
    driverDocumentImg: str
    driverPhotoImg: str

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


class UpdateDriverDetail(TrimmedBaseModel):
    DDID: str
    driverCity: str
    driverNumber: str
    driverPhotoImg: Optional[str] = None

class DeleteDriverDetail(TrimmedBaseModel):
    driverId: str
    deletedBy: Optional[str] = None
    reason: Optional[str] = None

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