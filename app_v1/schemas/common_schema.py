from pydantic import BaseModel, field_validator
from typing import Any, Dict, List, Optional, Union


class UploadSupportDocsRequest(BaseModel):
    file: Union[List[str], str]

    @field_validator("file", mode="before")
    @classmethod
    def normalize_file_input(cls, v):
        if isinstance(v, str):
            import json
            try:
                decoded = json.loads(v)
                if isinstance(decoded, list):
                    return decoded
            except Exception:
                pass
        return v


class UploadSupportDocsResponse(BaseModel):
    status: str
    urls: List[str] = []


class UploadSupportDocsErrorResponse(BaseModel):
    status: str
    message: str


class GenerateBlobSasRequest(BaseModel):
    blobPath: str


class GenerateBlobSasResponse(BaseModel):
    message: str
    sasUrl: str

class SendNotificationToAllDriversRequest(BaseModel):
    title: str
    message: str
    url: Optional[str] = ""
    soundFile: Optional[str] = "normal_notification"
    source: Optional[str] = ""
    destination: Optional[str] = ""
    travelDate: Optional[str] = ""
    pickupTime: Optional[str] = ""


class SendNotificationToAllDriversResponse(BaseModel):
    status: str
    totalProcessed: int
    totalSuccess: int
    response: Dict[str, Any]


class SendMarketingNotificationToNumbersRequest(BaseModel):
    title: str
    body: str
    url: Optional[str] = ""
    soundFile: Optional[str] = "normal_notification"
    type: Optional[str] = "default"
    phoneNumber: Union[str, List[str]]

    @field_validator("phoneNumber", mode="before")
    @classmethod
    def normalize_phone_number_input(cls, v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return []


class SendMarketingNotificationToNumbersResponse(BaseModel):
    status: str
    totalProcessed: int
    totalSuccess: int
    totalFailed: int
    noTokenIds: List[str]
    failedIds: List[str]


class SendMarketingNotificationToAllUsersRequest(BaseModel):
    title: str
    message: str
    url: Optional[str] = ""
    soundFile: Optional[str] = "normal_notification"
    type: Optional[str] = "default"


class SendMarketingNotificationToAllUsersResponse(BaseModel):
    status: str
    totalProcessed: int
    totalSuccess: int
    response: Optional[Dict[str, Any]] = None

class SendNotificationToSelectedDriversRequest(BaseModel):
    title: str
    message: str
    url: Optional[str] = ""
    soundFile: Optional[str] = "normal_notification"
    driverIds: List[str]

    @field_validator("driverIds", mode="before")
    @classmethod
    def normalize_driver_ids(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class SendNotificationToSelectedDriversResponse(BaseModel):
    status: str
    totalProcessed: int
    totalSuccess: int
    results: Dict[str, Any]