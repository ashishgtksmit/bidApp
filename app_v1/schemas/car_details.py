from pydantic import BaseModel, field_validator
from typing import Optional, List
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
    

class NoCarDetailsResponse(TrimmedBaseModel):
    message : str