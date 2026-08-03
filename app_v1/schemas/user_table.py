from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Literal, Optional,List,Union,Dict
from datetime import date, datetime

import re


class TrimmedBaseModel(BaseModel):
    """Automatically trims whitespace from all string fields"""
    @field_validator('*', mode='before')
    def strip_whitespace(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v
    
class User(TrimmedBaseModel):
    userAppId : str
    fullName : str
    emailId : EmailStr
    
    city : Optional[str] = None
    alternateNumber : Optional[str] = None
    dob : Optional[str] = None
    gender : Optional[str] = None
    profilePicture : Optional[str] = None
    customerRating : Optional[str] = "5"
    rating : Optional[str] = "5"
    totalNoOfReviews : Optional[int] = 0    
    totalCustomerRevies : Optional[int] = 0
    fcmToken : Optional[str] = None
    joiningDate : Optional[date] = None
    baseLocation : Optional[str] = None
    user_login_status : Optional[str] = None
    alsoVendor : Optional[bool] = False
    vendorApproved : Optional[bool] = False
    lockApp : Optional[bool] = False
    tags : Optional[str] = None
    noOfTripsCompleted : Optional[int] = 0
    deletionReason : Optional[str] = None
    bankAccountHolderName : Optional[str] = None
    bankAccountNo : Optional[str] = None
    bankIFSC : Optional[str] = None
    bankName : Optional[str] = None
    imageAadhar : Optional[str] = None
    imagePAN : Optional[str] = None
    imageBankAccount : Optional[str] = None
    regionPreferences : Optional[str] = None
    cityPreferences : Optional[str] = None
    requestTypePreferences : Optional[str] = None
    tableTimestamp : Optional[datetime] = None

    model_config = {"from_attributes": True}


class UserCreate(User):
    password : str


class UserUpdate(User):
    password : Optional[str] = None

class UserDelete(TrimmedBaseModel):
    """PR24 authenticated self-account deletion body.

    JWT ``sub`` is authoritative. Optional transitional ``userAppId`` must equal
    JWT ``sub`` when provided; Flutter must not send it.
    """

    password: str = Field(..., min_length=1)
    deletionReason: str = Field(..., min_length=3, max_length=500)
    userAppId: Optional[str] = None

    @field_validator("password", mode="before")
    @classmethod
    def validate_password(cls, v):
        text = str(v or "").strip()
        if not text:
            raise ValueError("password required")
        return text

    @field_validator("deletionReason", mode="before")
    @classmethod
    def validate_deletion_reason(cls, v):
        if v is None:
            raise ValueError("deletionReason required")
        text = str(v).strip()
        if len(text) < 3:
            raise ValueError("deletionReason too short")
        if len(text) > 500:
            raise ValueError("deletionReason too long")
        return text

    @field_validator("userAppId", mode="before")
    @classmethod
    def validate_optional_user_app_id(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        return text or None

class UserResponse(User):
    UID : int
    tableTimestamp : datetime 

    model_config = {"from_attributes": True}
    
class NoUserResponse(TrimmedBaseModel):
    message : str
    
    model_config = {"from_attributes": True}

class BidderDetail(TrimmedBaseModel):
    FULLNAME : str
    PRIMARYNUMBER : int
    ALTERNATENUMBER : str
    EMAILID : EmailStr
    DOB : Optional[date] = None
    CITY : str
    RATING : float
    TOTALNOOFREVIEWS : int
    JOININGDATE : Optional[date] = None
    BIDDERID : int
    BIDDERAMOUT : float
    PROFILEPIC : Optional[str]
    TAGS : List[str] = None
    NOOFTRIPSCOMPLETED : Optional[int] = None
    CARID : Optional[int] = None
    CARREGNO : Optional[str] = None
    CARMODEL : Optional[str] = None
    MODELYEAR : Optional[str] = None               
    CARCOLOR : Optional[str] = None                  
    OWNERNAME : Optional[str] = None
    REGISTRATIONDOC :Optional[str] = None
    POWEROFATTORNEYDOC :Optional[str] = None
    REGISTEREDON :Optional[date] = None
    ADMINAPPROVED : Optional[bool] = None
    CAROWNEDBYSAMEVENDOR : Optional[bool] = None
    CTD : Optional[int] = None
    IMAGEVEHICLEFRONT : Optional[str] = None
    IMAGEVEHICLESIDE : Optional[str] = None
    CAR_USERAPPID : Optional[str] = None
    CAR_TYPE : Optional[str] = None
    CAR_SUB_TYPE : Optional[str] = None
    CAPACITY : Optional[str] = None
    CAR_TYPE_IMAGE_URL : Optional[str] = None

    model_config = {
            "from_attributes":True,
            "json_encoders": {
                datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
            }
        }
                    

class UserBankDetailsUpdate(TrimmedBaseModel):
    """PR17 mobile bank text update.

    All four bank fields are required (no partial update on this route).
    Optional legacy ``userAppId`` is ignored for ownership; mismatch → 403.
    """

    bankAccountHolderName: str = Field(..., min_length=1)
    bankAccountNo: str = Field(..., min_length=1)
    bankIFSC: str = Field(..., min_length=1)
    bankName: str = Field(..., min_length=1)
    # Transitional only — JWT sub is authoritative.
    userAppId: Optional[str] = None

    @field_validator("bankAccountHolderName", "bankName")
    @classmethod
    def validate_required_text(cls, v):
        text = str(v or "").strip()
        if not text:
            raise ValueError("ERROR_REQUIRED_FIELD")
        return text

    @field_validator("bankIFSC")
    @classmethod
    def validate_ifsc(cls, v):
        if not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", str(v).upper()):
            raise ValueError("ERROR_INVALID_IFSC")
        return str(v).upper()

    @field_validator("bankAccountNo")
    @classmethod
    def validate_bank_account_no(cls, v):
        cleaned = str(v).replace(" ", "").strip()
        if not re.match(r"^[0-9A-Za-z\-]{6,22}$", cleaned):
            raise ValueError("ERROR_INVALID_ACCOUNTNO")
        return cleaned

    @field_validator("userAppId")
    @classmethod
    def validate_user_app_id(cls, v):
        if v is None:
            return None
        if not str(v).strip():
            return None
        return str(v).replace(" ", "").strip()


class UserBankDetailsResponse(TrimmedBaseModel):
    """Legacy uppercase bank payload (non-mobile / admin consumers)."""

    BANK_AC_HOLDER: Optional[str] = None
    BANK_AC_NO: Optional[str] = None
    BANK_IFSC: Optional[str] = None
    BANK_NAME: Optional[str] = None

    model_config = {"from_attributes": True}


class VendorBankAccountSummaryResponse(TrimmedBaseModel):
    """PR17 mobile GET bank summary — masked account only."""

    hasBankAccount: bool
    maskedAccountNumber: Optional[str] = None
    accountHolderName: Optional[str] = None
    bankIFSC: Optional[str] = None
    bankName: Optional[str] = None

    model_config = {"from_attributes": True}

class UserLogin(TrimmedBaseModel):
    userAppId : str
    password : str
    fcmToken : Optional[str] = None


class OtpVerifyRequest(TrimmedBaseModel):
    userAppId: str
    otp: str


class OtpVerifyResponse(TrimmedBaseModel):
    message: str
    reset_token: str


class LoginResponse(TrimmedBaseModel):
    message : str
    user : List[dict]

class LogoutResponse(TrimmedBaseModel):
    messsage : str
    status : str
    userAppId : str

class UserImageUpload(TrimmedBaseModel):
    """PR23 profile-image upload body.

    JWT ``sub`` is authoritative. ``userAppId`` is optional transitional
    compatibility only and must equal JWT ``sub`` when provided.
    """

    image: str = Field(..., min_length=1)
    name: Optional[str] = None
    userAppId: Optional[str] = None


# Alias retained for OpenAPI / callers preferring the PR23 name.
ProfileImageUploadRequest = UserImageUpload

class VendorUpdate(TrimmedBaseModel):
    alsoVendor : bool
    userAppId: str = Field(..., min_length=1)
    registration: str = Field(..., min_length=1)
    carregno: str = Field(..., min_length=1)
    carmodel: str = Field(..., min_length=1)
    modelyear: str = Field(..., min_length=1)
    ownername: str = Field(..., min_length=1)  
    
class VendorResponse(TrimmedBaseModel):
    userAppId: str
    fcmToken : Optional[str] = None


class VendorKycCreate(TrimmedBaseModel):
    """Vendor KYC / registration body (PR16).

    Applicant identity and lifecycle flags are server-derived from JWT.
    Legacy clients may still send optional userAppId / alsoVendor; ownership
    and alsoVendor are not client-authoritative.
    """

    dob: str = Field(..., min_length=1)
    gender: str = Field(..., min_length=1)
    addressLine1: str = Field(..., min_length=1)
    addressLine2: str = Field(..., min_length=1)
    city: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    bankAccountHolderName: str = Field(..., min_length=1)
    bankAccountNo: str = Field(..., min_length=1)
    bankIFSC: str = Field(..., min_length=1)
    bankName: str = Field(..., min_length=1)
    imageAadhar: str = Field(..., min_length=1)
    imagePAN: str = Field(..., min_length=1)
    imageBankAccount: str = Field(..., min_length=1)
    # Legacy / ignored — JWT sub is authoritative; alsoVendor forced server-side.
    userAppId: Optional[str] = None
    alsoVendor: Optional[bool] = None

    @field_validator("userAppId")
    @classmethod
    def validate_user_app_id(cls, v):
        if v is None:
            return None
        if not str(v).strip():
            raise ValueError("ERROR_INVALID_USERAPPID")
        return str(v).replace(" ", "").strip()

    @field_validator("dob")
    @classmethod
    def validate_dob(cls, v):
        # Canonical wire format for PR16: yyyy-MM-dd only.
        try:
            datetime.strptime(str(v).strip(), "%Y-%m-%d")
            return str(v).strip()
        except ValueError:
            raise ValueError("ERROR_INVALID_DOB")

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v):
        mapping = {
            "M": "Male",
            "MALE": "Male",
            "F": "Female",
            "FEMALE": "Female",
            "O": "Other",
            "OTHER": "Other",
        }
        key = str(v or "").strip().upper()
        if key not in mapping:
            raise ValueError("ERROR_INVALID_GENDER")
        return mapping[key]

    @field_validator("bankIFSC")
    @classmethod
    def validate_ifsc(cls, v):
        if not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", str(v).upper()):
            raise ValueError("ERROR_INVALID_IFSC")
        return str(v).upper()

    @field_validator("bankAccountNo")
    @classmethod
    def validate_bank_account_no(cls, v):
        cleaned = str(v).replace(" ", "")
        if not re.match(r"^[0-9A-Za-z\-]{6,22}$", cleaned):
            raise ValueError("ERROR_INVALID_ACCOUNTNO")
        return cleaned

    @field_validator("imageAadhar", "imagePAN", "imageBankAccount")
    @classmethod
    def validate_images(cls, v):
        if not v or str(v).strip() == "":
            raise ValueError("ERROR_MISSING_IMAGES")
        return v

    model_config = {"from_attributes": True}

class UpdateRequestTypeSelectionsRequest(TrimmedBaseModel):
    """PR18 PUT /updaterequesttypeselections body.

    JWT ``sub`` is authoritative. Optional transitional ``userAppId`` must match
    JWT when provided; it never selects the target row.
    """

    userAppId: Optional[str] = None
    requestTypeIds: List[Union[str, int]]

    @field_validator("userAppId", mode="before")
    @classmethod
    def normalize_optional_user_app_id(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        return text or None

    @field_validator("requestTypeIds", mode="before")
    @classmethod
    def parse_request_type_ids(cls, v):
        if v is None:
            raise ValueError("ERROR_INVALID_REQUESTTYPEIDS")
        if isinstance(v, list):
            for item in v:
                if isinstance(item, bool) or isinstance(item, float):
                    raise ValueError("ERROR_INVALID_REQUESTTYPEIDS")
            return v
        if isinstance(v, str):
            if v.strip() == "":
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        raise ValueError("ERROR_INVALID_REQUESTTYPEIDS")

    model_config = {"from_attributes": True}


class UpdateRegionCitySelectionsRequest(TrimmedBaseModel):
    """PR18 PUT /updateregioncityselections body.

    Both ``regionIds`` and ``cityIds`` are required (may be empty arrays).
    JWT ``sub`` is authoritative. Optional transitional ``userAppId`` must match
    JWT when provided; it never selects the target row.
    """

    userAppId: Optional[str] = None
    regionIds: List[Union[str, int]]
    cityIds: List[Union[str, int]]

    @field_validator("userAppId", mode="before")
    @classmethod
    def normalize_optional_user_app_id(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        return text or None

    @field_validator("regionIds", "cityIds", mode="before")
    @classmethod
    def parse_ids(cls, v):
        if v is None:
            raise ValueError("ERROR_MISSING_ID_ARRAY")
        if isinstance(v, list):
            for item in v:
                if isinstance(item, bool) or isinstance(item, float):
                    raise ValueError("ERROR_INVALID_ID_VALUE")
            return v
        if isinstance(v, str):
            if v.strip() == "":
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        raise ValueError("ERROR_MISSING_ID_ARRAY")

    model_config = {"from_attributes": True}

class RequestTypeResponse(TrimmedBaseModel):
    REQUEST_TYPE_ID: int
    REQUEST_TYPE_NAME: str
    SELECTED: bool

    model_config = {"from_attributes": True}

# class GetRequestTypeSelectionsResponse(TrimmedBaseModel):
#     message: str | None = None
#     data: List[RequestTypeResponse] | None = None

#     model_config = {"from_attributes": True}
    


class GetUserDetailsResponse(TrimmedBaseModel):
    """Authenticated profile row for GET /getuserdetails (and related list endpoints).

    Session-critical fields (PR6):
    - USERAPPID: canonical app user id / phone
    - ALSOVENDOR / VENDOR: whether the user can operate in vendor mode
    - CUSTOMERRATING / TOTALCUSTOMERRATING: passenger ratings
    - VENDORRATING / TOTALVENDORRATING: vendor ratings (null for non-vendors)

    Do not overload RATING for both customer and vendor ratings.
    RATING / TOTALREVIEWS remain for legacy consumers and mirror vendor rating columns.
    """
    USERAPPID : Optional[str] = None
    ALTERNATEMNUM:Optional[str] = None
    FULLNAME:str
    EMAILID:EmailStr
    EMAIL:Optional[EmailStr] = None
    DOB:str
    CITY:str
    GENDER:Optional[str] = None
    PROFILEPIC:Optional[str] = None
    RATING:Optional[float] = None
    TOTALREVIEWS:Optional[int] = None
    CUSTOMERRATING:Optional[str] = None
    TOTALCUSTOMERRATING:Optional[int] = None
    VENDORRATING:Optional[float] = None
    TOTALVENDORRATING:Optional[int] = None
    FCMTOKEN:Optional[str] = None
    USERLOGINSTATUS:Optional[str] = None
    ALSOVENDOR:Optional[bool] = None
    VENDOR:Optional[bool] = None
    TABLETIMESTAMP:Optional[datetime] = None

    model_config = {
        "from_attributes": True,
        "json_encoders": {
            datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
        }
    }

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds

class LoginResponseWithTokens(BaseModel):
    # Your original login shape (message + user list[dict]) + tokens
    message: str
    user: List[Dict]
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

class RefreshRequest(BaseModel):
    refresh_token: str


# -------- NEW: used only for WebSocket auth -------- #

class WsAuthRequest(BaseModel):
    token: str        # access token
    client_id: str    # X-Client-Id
    flag: str         # "Customer" or "Vendor"


class WsAuthResponse(BaseModel):
    appid: str        # userAppId / phone (comes from DB, not client)
    flag: str         # normalized "Customer" / "Vendor"
    exp: int          # unix timestamp from JWT
    

class CustomerListItem(BaseModel):
    UID :int
    USERAPPID : int
    ALTERNATENUMBER : Optional[str] = None
    FULLNAME : Optional [str] = None
    EMAILID : Optional [str] = None
    DOB : Optional [str] = None
    CITY : Optional [str] = None
    GENDER : Optional [str] = None
    PROFILEPICTURE : Optional[str] = None
    CUSTOMERRATING : Optional[str] = None
    TOTALCUSTOMERREVIEWS : Optional[str] = None
    FCMTOKEN : Optional[str] = None
    JOININGDATE : Optional[date] = None
    CUSTSIGNUPDATE : Optional[int] = None
    CUSTNOOFTRIPSCOMPLETED : Optional[int] = None
    BASELOCATION : Optional[str] = None
    USERLOGINSTATUS : Optional[str] = None
    LOCKAPP : Optional[bool] = None
    TABLETIMESTAMP : Optional[datetime] = None
    model_config = {
        "from_attributes": True,
        "json_encoders": {
            datetime: lambda v: v.strftime('%Y-%m-%d %H:%M:%S') if v else None
        }
    }

class GetAllVendorsWithUnapprovedResponse(BaseModel):
    UID : int
    USERAPPID : Optional[str] = None
    FULLNAME : Optional[str] = None
    EMAILID : Optional[str] = None
    ALTERNATENUMBER : Optional[str] = None
    DOB : Optional[str] = None
    GENDER: Optional[str] = None
    PROFILEPICTURE: Optional[str] = None
    RATING: Optional[float] = None
    TOTALNOOFREVIEWS: Optional[int] = None
    BASELOCATION: Optional[str] = None
    USERLOGINSTATUS: Optional[int] = None
    ALSOVENDOR: Optional[int] = None
    VENDORAPPROVED: Optional[int] = None
    LOCKAPP: Optional[int] = None
    NOOFTRIPSCOMPLETED: Optional[int] = None
    ADDRESS: Optional[str] = None
    STATE: Optional[str] = None
    BANKACCOUNTHOLDERNAME: Optional[str] = None
    BANKACCOUNTNO: Optional[str] = None
    BANKIFSC: Optional[str] = None
    BANKNAME: Optional[str] = None
    IMAGEAADHAR: Optional[str] = None
    IMAGEPAN: Optional[str] = None
    IMAGEBANKACCOUNT: Optional[str] = None

    REGIONPREFERENCES: Optional[str] = None
    CITYPREFERENCES: Optional[str] = None
    REQUESTTYPEPREFERENCES: Optional[str] = None

    # Human readable names (most important for frontend)
    REGIONPREFERENCE_NAMES: Optional[str] = None
    CITYPREFERENCE_NAMES: Optional[str] = None
    REQUESTTYPEPREFERENCENAMES: Optional[str] = None

    TABLETIMESTAMP: Optional[datetime] = None


    model_config = {"from_attributes": True}


class AdminNumberResponse (BaseModel):
    phonenumber : str

    model_config = {"from_attributes":True}


class UpdateVendorApprovalRequest(BaseModel):
    UID: int
    vendorApproved: bool

class UpdateVendorLockAppStatusRequest(BaseModel):
    UID: int
    lockApp: bool

class RejectUserRequest(BaseModel):
    userid:int
    deletedBy : Optional[str] = None
    reason : Optional[str] = None


class UploadVendorDocumentRequest(BaseModel):
    vendorid: str
    docType: Literal["PROFILEPICTURE", "IMAGEAADHAR", "IMAGEPAN", "IMAGEBANKACCOUNT"]
    uploadFile: Union[str, List[str]]

    @field_validator("vendorid", mode="before")
    @classmethod
    def normalize_vendorid(cls, v):
        if isinstance(v, list):
            return str(v[0]).strip() if v else ""
        return str(v).strip()

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
    
class UploadVendorDocumentResponse(BaseModel):
    status : str
    docType : str
    column : str
    vendor : str
    url : str



