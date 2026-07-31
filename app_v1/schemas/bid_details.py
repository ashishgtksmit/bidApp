from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date


class Bids(BaseModel):
    bidAmount : int
    bidStatus : str

    model_config = {"from_attributes":True}


class BidInsert(Bids):
    RID : int
    bidderID : int
    bidAmount : int
    assignedVehicleID : str    

class BidUpdate(BaseModel):
    bidAmount : Optional[int] = None
    bidStatus : Optional[str] = None

class BidDelete(Bids):
    rId : int
    bidderID : int


class BidDetail(BaseModel):
    BIDID : int
    BIDDERID : int
    BIDAMOUNT : float
    BIDDONEON : datetime
    BIDDERNAME : str
    BIDDERRATING : float
    TOTALNOOFREVIEWS : int
    FCMTOKEN : str
    PROFILEPIC : Optional[str]
    BIDSTATUS : Optional[str] = None
    DOB : date
    JOININGDATE : Optional[date] = None
    BASELOCATION : str
    TAGS : List[str]
    NOOFTRIPSCOMPLETED : int
    CARID : Optional[int]=None
    CARREGNO : Optional[str]=None
    CARMODEL: Optional[str]=None
    MODELYEAR: Optional[str]=None
    CARCOLOR: Optional[str]=None
    OWNERNAME: Optional[str]=None
    REGISTRATIONDOC: Optional[str]=None
    POWEROFATTORNEYDOC: Optional[str]=None
    REGISTEREDON: Optional[datetime]=None
    ADMINAPPROVED: Optional[bool]=None
    CAROWNEDBYSAMEVENDOR: Optional[bool]=None
    CTD: Optional[int]=None
    IMAGEVEHICLEFRONT: Optional[str]=None
    IMAGEVEHICLESIDE: Optional[str]=None
    CAR_USERAPPID: Optional[str]=None
    CAR_TYPE: Optional[str]=None
    CAR_SUB_TYPE: Optional[str]=None
    CAPACITY: Optional[str]=None
    CAR_TYPE_IMAGE_URL: Optional[str]=None

    model_config = {
        "from_attributes": True,
        "json_encoders": {
            datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
        }
    }

    # model_config = {"from_attributes":True}

class UpdateCarIdForBidRequest(BaseModel):
    BID : int
    CARID : int
    
class NoBidResponse(BaseModel):
    message : str


class CustomerBidDetail(BaseModel):
    """Customer-safe bid list item for GET /getallbidsforrequest (PR10).

    Excludes FCMTOKEN and unused KYC/document fields.
    """

    BIDID: int
    BIDDERID: str
    BIDAMOUNT: float
    BIDSTATUS: Optional[str] = None
    BIDDERNAME: Optional[str] = None
    BIDDERRATING: float = 0.0
    TOTALNOOFREVIEWS: int = 0
    PROFILEPIC: Optional[str] = None
    DOB: Optional[date] = None
    JOININGDATE: Optional[date] = None
    BASELOCATION: Optional[str] = None
    TAGS: List[str] = []
    NOOFTRIPSCOMPLETED: int = 0
    CARID: Optional[int] = None
    CARREGNO: Optional[str] = None
    CARMODEL: Optional[str] = None
    MODELYEAR: Optional[str] = None
    CARCOLOR: Optional[str] = None
    OWNERNAME: Optional[str] = None
    REGISTEREDON: Optional[str] = None
    IMAGEVEHICLEFRONT: Optional[str] = None
    IMAGEVEHICLESIDE: Optional[str] = None
    CAR_TYPE: Optional[str] = None
    CAR_SUB_TYPE: Optional[str] = None

    model_config = {"from_attributes": True}