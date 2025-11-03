from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime,time,date


class CustomerReviewBase(BaseModel):
    generalRating : Optional[str] = None
    comments : Optional[str] = None

    model_config={"from_attributes":True}

class CreateCustomerReview(BaseModel):
    RID : int
    VENDORID : int
    CUSTOMERID : int
    RATING : float
    COMMENTS : Optional[str] = None

    @field_validator('RID','VENDORID','CUSTOMERID')
    @classmethod
    def not_empt(cls, v):
        if not v:
            raise ValueError('FIELD_EMPTY_ERROR')
        return v
    
    @field_validator('RATING')
    @classmethod
    def validate_rating(cls,v):
        if v is None or not (0<= v <= 5):
            raise ValueError("RATING_FIELD_ERROR")
        return v
    
    model_config={"from_attributes":True}

class UpdateCustomerReview(BaseModel):
    generalRating : Optional[str] = None
    comments : Optional[str] = None

    model_config={"from_attributes":True}

class CustomerReviewDetail(BaseModel):
    RID : int
    ratingGiverUserAppId : Optional[str] = Field(alias="ratingGivenBy")
    ratingReceiverUserAppId : Optional[str] = Field(alias="ratingReceivedBy")
    vendorFullName: Optional[str] = None
    vendorProfilePicture: Optional[str] = None
    fromLocation: Optional[str] = None
    fromLandmark: Optional[str] = None
    toLocation: Optional[str] = None
    toLandmark: Optional[str] = None
    pickUpDate: Optional[date] = None
    pickUpTime: Optional[time] = None
     
    model_config={"from_attributes":True}

class NoReviewResponse(BaseModel):
    message : str



