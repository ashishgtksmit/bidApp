from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal, InvalidOperation


class Bids(BaseModel):
    bidAmount : int
    bidStatus : str

    model_config = {"from_attributes":True}


class BidInsert(Bids):
    """Legacy insert schema — prefer VendorBidInsert for mobile PR11."""

    RID : int
    bidderID : int
    bidAmount : int
    assignedVehicleID : str


def _validate_positive_bid_amount(v):
    if v is None:
        raise ValueError("bidAmount is required")
    try:
        amount = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("bidAmount must be numeric")
    if not amount.is_finite():
        raise ValueError("bidAmount must be finite")
    if amount <= 0:
        raise ValueError("bidAmount must be greater than zero")
    # DECIMAL(11,2) compatible
    quantized = amount.quantize(Decimal("0.01"))
    if amount.as_tuple().exponent is not None and amount.as_tuple().exponent < -2:
        amount = quantized
    if abs(amount) >= Decimal("1000000000"):
        raise ValueError("bidAmount exceeds storage limits")
    return float(amount)


class VendorBidInsert(BaseModel):
    """Mobile insert body (PR11). Identity/status derived server-side from JWT."""

    RID: int
    CARID: int
    bidAmount: float

    @field_validator("bidAmount", mode="before")
    @classmethod
    def validate_amount(cls, v):
        return _validate_positive_bid_amount(v)


class BidAmountUpdate(BaseModel):
    """Body for PUT /updatebid (PR11)."""

    bidAmount: float

    @field_validator("bidAmount", mode="before")
    @classmethod
    def validate_amount(cls, v):
        return _validate_positive_bid_amount(v)


class VendorRejectBody(BaseModel):
    """Body for PUT /rejectrequestbyvendor (PR11)."""

    rejectionReason: str = Field(..., min_length=1, max_length=2000)

    @field_validator("rejectionReason", mode="before")
    @classmethod
    def trim_reason(cls, v):
        if v is None:
            raise ValueError("rejectionReason is required")
        text = str(v).strip()
        if not text:
            raise ValueError("rejectionReason must not be empty")
        if len(text) > 2000:
            raise ValueError("rejectionReason exceeds maximum length")
        return text


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


class VendorBidDetail(BaseModel):
    """Vendor-safe bid list item for GET /getallbidsforrequestforvendor (PR11).

    Same visible field set as customer View Bids UI needs. No FCMTOKEN.
    """

    BIDID: int
    BIDDERID: str
    BIDAMOUNT: float
    BIDSTATUS: Optional[str] = None
    BIDDERNAME: Optional[str] = None
    BIDDERRATING: float = 0.0
    TOTALNOOFREVIEWS: int = 0
    PROFILEPIC: Optional[str] = None
    JOININGDATE: Optional[date] = None
    TAGS: List[str] = []
    CARID: Optional[int] = None
    CARREGNO: Optional[str] = None
    CARMODEL: Optional[str] = None

    model_config = {"from_attributes": True}


class VendorCarSummaryResponse(BaseModel):
    """Lean approved-car row for vendor bidding UI (PR11). No KYC/docs."""

    CARID: int
    CARREGNO: Optional[str] = None
    CARMODEL: Optional[str] = None
    VEHICLE_FRONT: Optional[str] = None
    CAR_TYPE: Optional[str] = None

    model_config = {"from_attributes": True}
