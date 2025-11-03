from pydantic import BaseModel
from typing import Optional
from datetime import datetime,date,time


class Review(BaseModel):
    driverBehaviour : int
    punctuality : int
    carCondition : int
    cleanliness : int
    refreshments : int
    comments : Optional[str] = None

    model_config={"from_attributes":True}

class ReviewCreate(Review):
    customerAppId : str
    RID : int
    VENDORID : int

class ReviewUpdate(BaseModel):
    driverBehaviour : Optional[int] = None
    punctuality : Optional[int] = None
    carCondition : Optional[int] = None
    cleanliness : Optional[int] = None
    refreshments : Optional[int] = None
    comments : Optional[int] = None   
    

class ReviewDetail(BaseModel):
    CUSTOMERID : str
    CUSTOMERNAME : str
    REQUESTID : int
    VENDORID : int
    DRIVERBEHAVIOUR : int
    PUNCTUALITY : int
    CARCONDITION : int
    CLEANLINESS : int
    REFRESHMENTS : int
    COMMENTS : str
    CUSTOMER_PROFILEPIC : Optional[str] = None
    REQ_FROMLOCATION : str
    REQ_FROMLANKDMARK : str
    REQ_TOLOCATION : Optional[str] = None
    REQ_TOLANDMARK : Optional[str] = None
    REQ_PICKUPDATE : Optional[date] = None
    REQ_PICKUPTIME : Optional[time] = None
    REQ_NOOFADULTS : Optional[int] = None
    REQ_NOOFKIDS : Optional[int] = None
    REQ_CARTYPE : Optional[str] = None
    REQ_ACREQUEST : Optional[bool] = None
    REQ_CARRIERREQUEST : Optional[bool] = None
    REQ_SPECIALREQUEST : Optional[str] = None
    BID_BIDAMOUNT : Optional[float] = None
    BID_CARID : Optional[int] = None
    CAR_REGNO : Optional[str] = None
    CAR_MODEL : Optional[str] = None
    CAR_MODELYEAR : Optional[str] = None
    CAR_COLOR : Optional[str] = None
    CAR_OWNERNAME : Optional[str] = None
    CARTYPE : Optional[str] = None
    DRIVER_NAME : Optional[str] = None
                    
                    

    model_config={
        "from_attributes":True,
        "json_encoders": {
            datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
        }
    }

class NoReviewResponse(BaseModel):
    message : str

