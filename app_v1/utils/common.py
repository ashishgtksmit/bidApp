from pydantic import BaseModel,EmailStr
from datetime import datetime
from typing import Any,Optional,List


def _to_id_array(value) -> List[int]:
    """Convert string or list to deduped, sorted list of ints"""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [x.strip() for x in str(value).split(',') if x.strip()]
    ints = []
    seen = set()
    for v in raw:
        if v.isdigit() and int(v) not in seen:
            seen.add(int(v))
            ints.append(int(v))
    ints.sort()
    return ints

def _ids_to_csv(ids: List[int]) -> str:
    return ','.join(map(str, ids))

def _csv_to_set(csv: str) -> set:
    if not csv:
        return set()
    return {int(x) for x in csv.split(',') if x.strip().isdigit()}


def parse_dob(dob_str):
    if not dob_str:
        return None
    
    for fmt in ("%d-%m-%Y","%Y-%m-%d"):
        try:
            return datetime.strptime(dob_str,fmt).date()
        except ValueError:
            continue
    return None


@staticmethod
def alias_generator(field_name: str) -> str:
            return field_name.upper()

class UppercaseBase(BaseModel):
     model_config = {
          "from_attributes":True,
          "alias_generator":alias_generator,
          "populate_by_name":True
     }


class ErrorResponse(BaseModel):
     message : str

# Schema for SMS input
class SMSSend(BaseModel):
    to: str
    body: str

# Response schema for successful SMS
class SMSResponse(BaseModel):
    message: str
    details: Any

# Response schema for SMS errors
class SMSErrorResponse(BaseModel):
    message: str
    error: Optional[str] = None

# Schema for email input
class EmailSend(BaseModel):
    message: str
    subject: str
    from_address: EmailStr
    from_name: str
    to_address: EmailStr
    to_name: str
    cc_address: Optional[EmailStr] = None
    cc_name: Optional[str] = None
    bcc_address: Optional[EmailStr] = None
    bcc_name: Optional[str] = None
    attachment_path: Optional[str] = None


# Response schema for email errors
class EmailErrorResponse(BaseModel):
    message: str
    error: Optional[str] = None

class ImageResponse(BaseModel):
    message: str
    url: Optional[str] = None


# Schema for FCM input
class FCMSend(BaseModel):
    title: str
    body: str
    fcmToken: Optional[str] = None
    userAppId : Optional[str] = None    
    url: Optional[str] = None
    source: Optional[str] = None
    destination: Optional[str] = None
    travelDate: Optional[str] = None
    pickupTime: Optional[str] = None    
    type: Optional[str] = None
    soundFile: Optional[str] = None

class FCMSendDrivers(FCMSend):
     driverIds : List[str]

class SendNotificationResponse(BaseModel):
    status: str
    totalProcessed: int
    totalSuccess: int
    results: dict  # { "userAppId": {"sent": True/False, "reason": "..."} }
