from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import date,time,datetime


# -------------------------------- #
# 🔹 Base Schema (shared fields).  #
# ------------------------------- #

class RequestBase(BaseModel):
    fromLocation : str
    fromLandmark : str
    toLocation : str
    toLandmark : str
    pickUpDate : date
    pickUpTime : time
    noOfAdults : int
    noOfKids : int
    carType : str
    acRequest : bool
    carrierRequest : bool
    specialRequest : Optional[str] = None
    bidEndTime : Optional[datetime] = None
    requestType : Optional[int] = None
    driverAssignedId : Optional[int] = None

    model_config = {"from_attributes": True}

# ---------------------------------- #
# 🔹 Insert Schema                   #
# --------------------------------- #

class RequestCreate(RequestBase):
    wizzpnr : Optional[str] = None
    customerAppId : str
    requestStatus : Optional[str] = None


# ---------------------------------- #
# 🔹 PR12 Cancel / Reopen Schemas     #
# --------------------------------- #

class CancelBookingBody(BaseModel):
    """Body for PUT /bookingcancelledbyuser. RID is query-only."""
    rejectionReason: str = Field(..., min_length=1)


class ReopenBookingResponse(BaseModel):
    """Success/error wrapper for PUT /reopenbooking."""
    message: str
    newRequestId: Optional[int] = None
    error: Optional[str] = None


class CustomerBookingVendorDetail(BaseModel):
    """
    Customer-safe vendor + selected-car summary for GET /getvendordetailsbyrid.

    Excludes FCM tokens, KYC/registration/POA docs, bank data, and approval internals.
    """
    FULLNAME: Optional[str] = None
    PRIMARYNUMBER: Optional[str] = None
    DOB: Optional[date] = None
    CITY: Optional[str] = None
    GENDER: Optional[str] = None
    RATING: Optional[float] = None
    TOTALNOOFREVIEWS: Optional[int] = None
    JOININGDATE: Optional[date] = None
    PROFILEPIC: Optional[str] = None
    TAGS: List[str] = []
    NOOFTRIPSCOMPLETED: Optional[int] = None
    CARID: Optional[int] = None
    CARREGNO: Optional[str] = None
    CARMODEL: Optional[str] = None
    MODELYEAR: Optional[str] = None
    IMAGEVEHICLEFRONT: Optional[str] = None
    IMAGEVEHICLESIDE: Optional[str] = None
    CAR_TYPE: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------- #
# 🔹 Update Schema                   #
# --------------------------------- #

class RequestUpdate(BaseModel):
    RID : int    
    fromLocation : Optional[str] = None
    fromLandmark : Optional[str] = None
    toLocation : Optional[str] = None
    toLandmark : Optional[str] = None
    pickUpDate : Optional[date] = None
    pickUpTime : Optional[time] = None
    noOfAdults : Optional[int] = None
    noOfKids : Optional[int] = None
    carType : Optional[str] = None
    acRequest : Optional[bool] = None
    carrierRequest : Optional[bool] = None
    specialRequest : Optional[str] = None
    bidEndTime : Optional[datetime] = None



# ---------------------------------- #
# 🔹 Delete Schema                    #
# --------------------------------- #

class RequestDelete(BaseModel):
    RID : int


# --------------------------------------- #
# 🔹 Response Schema (General Response).  #
# -------------------------------------- #

class RequestResponse(RequestBase):
    RID : int
    requestStatus : str
    paymentStatus : Optional[str] = None
    customerAppId : str
    requestWonBy : Optional[str] = None
    finalAmount : int
    noOfBids : int
    reviewDone : str
    tableTimestamp : datetime

    
    model_config = {"from_attributes": True}

# --------------------------------------- #
# 🔹 Special API Response Wrappers        #
# -------------------------------------- #

class InsertResponse(BaseModel):
    message : str

class UpdateResponse(BaseModel):
    status : str

class DeleteResponse(BaseModel):
    message : str

class NoBidsResponse(BaseModel):
    message : str


# Response Schema for getRIDByInputs
class RequestByRidResponse(BaseModel):
    RID : int

    model_config={"from_attributes" : True}


class RequestConfirmedCommonResponse(BaseModel):
    REQUESTID : int    
    FROMLOCATION : str
    FROMLANDMARK : str
    TOLOCATION : str
    TOLANDMARK : str
    PICKUPDATE : date
    PICKUPTIME : time
    NOOFADULTS : int
    NOOFKIDS : int
    CARTYPE:str
    ACREQUEST:bool
    CARRIERREQUEST:bool
    SPECIALREQUEST:str
    BIDENDTIME:datetime
    REQUESTSTATUS:str
    PAYMENTSTATUS:Optional[str] = None
    CUSTOMERAPPID:str
    REQUESTWONBY:Optional[str] = None
    NOOFBIDS : Optional[int] = None
    TABLETIMESTAMP : Optional[datetime] = None

    model_config = {
            "from_attributes":True,
            "json_encoders": {
                datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
            }
        }


class RequestConfirmedForUserResponse(RequestConfirmedCommonResponse):   
    FINALAMOUNT:float
    VENDORNAME:str
    VENDORCITY:str
    VENDORNUMBER:str
    VENDORALTNUMBER:str
    VENDORRATING:str
    VENDORTOTALREVIEWS:int

    model_config = {
            "from_attributes":True,
            "json_encoders": {
                datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
            }
        }

class RequestConfirmedForVendorResponse(RequestConfirmedCommonResponse):    
    USERFULLNAME:str
    CITY:str
    PHONENUMBER:str
    ALTNUMBER:str
    PROFILEPIC:Optional[str] = None
    BIDAMOUNT : Optional[float] = None
    CUSTREVIEW_GENERALRATING : Optional[float] = None
    CANCELLATIONREASON : Optional[str] = None

    model_config = {
            "from_attributes":True,
            "json_encoders": {
                datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
            }
        }


class AssignDriverRequest(BaseModel):
    """Body for PUT /updatedrivertorequest (PR13). RID + DRIVERID only."""

    RID: int
    DRIVERID: int

    @field_validator("RID", "DRIVERID", mode="before")
    @classmethod
    def coerce_positive_int(cls, v):
        if v is None:
            raise ValueError("Required")
        if isinstance(v, bool):
            raise ValueError("Invalid")
        if isinstance(v, int):
            return v
        text = str(v).strip()
        if not text or not text.lstrip("-").isdigit():
            raise ValueError("Invalid")
        return int(text)



class RequestForUserResponse(RequestConfirmedCommonResponse):
        FINALAMOUNT: float
        NOOFBIDS: int
        REJECTIONREASON:Optional[str] = None
        REOPENBOOKING:Optional[int] = None
        TABLETIMESTAMP:datetime
        REVIEWDONE:Optional[str] = None
        DRIVERNAME:Optional[str] = None
        DRIVERNUMBER:Optional[str] = None
        DRIVERPHOTO:Optional[str] = None
        DRIVERDOB:Optional[str] = None
        DRIVERGENDER:Optional[str] = None
        DRIVERCITY:Optional[str] = None
        DRIVERLICENSE:Optional[str] = None
        BIDAMOUNT:Optional[float] = None
        CARID:Optional[int] = None
        CARREGNO:Optional[str] = None
        CARMODEL:Optional[str] = None
        MODELYEAR:Optional[str] = None
        CARCOLOR:Optional[str] = None
        OWNERNAME:Optional[str] = None
        REGISTRATIONDOC:Optional[str] = None
        POWEROFATTORNEYDOC:Optional[str] = None
        REGISTEREDON:Optional[datetime] = None
        CAROWNEDBYSAMEVENDOR:Optional[bool] = None
        CTD:Optional[int] = None
        CAR_TYPE:Optional[str] = None

        model_config = {
            "from_attributes":True,
            "json_encoders": {
                datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
            }
        }

class GetBookingReportResponse(BaseModel):
    REQUESTID: int
    WIZZPNR: Optional[str] = None
    FROMLOCATION: str
    FROMLANDMARK: Optional[str] = None
    TOLOCATION: str
    TOLANDMARK: Optional[str] = None
    PICKUPDATE: date
    PICKUPTIME: time
    NOOFADULTS: int
    NOOFKIDS: int
    CARTYPE: str
    ACREQUEST: str
    CARRIERREQUEST: str
    BIDENDTIME: Optional[str] = None
    REQUESTSTATUS: str
    CUSTOMERAPPID: str
    REQUESTWONBY: Optional[str] = None
    FINALAMOUNT: Optional[float] = None
    NOOFBIDS: Optional[int] = None
    REJECTIONREASON: Optional[str] = None
    REQUESTOPENED: Optional[bool] = None
    REVIEWDONE: Optional[str] = None
    TABLETIMESTAMP: Optional[str] = None