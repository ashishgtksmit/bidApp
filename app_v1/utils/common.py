from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr
from datetime import datetime
from typing import Any,Optional,List


def _to_id_array(value) -> List[int]:
    """Convert string or list to deduped, sorted list of ints.

    Accepts JSON integers and digit-only strings. Silently skips malformed
    tokens for legacy callers. Prefer ``_parse_id_list_strict`` for the
    PR18 mobile preference contract (rejects malformed values).
    """
    if value is None:
        return []
    raw = value if isinstance(value, list) else [x.strip() for x in str(value).split(',') if x.strip()]
    ints: List[int] = []
    seen = set()
    for v in raw:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            parsed = v
        elif isinstance(v, float):
            if not v.is_integer():
                continue
            parsed = int(v)
        else:
            token = str(v).strip()
            if not token.isdigit():
                continue
            parsed = int(token)
        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        ints.append(parsed)
    ints.sort()
    return ints


def _parse_id_list_strict(value, *, field_name: str = "ids") -> List[int]:
    """Strict ID list parser for PR18 preference updates.

    Accepts:
    - JSON integers (> 0)
    - digit-only strings (backward compatibility)
    - empty lists

    Rejects:
    - booleans, floats, negatives, zero
    - arbitrary strings, nested arrays/objects
    - missing / non-list values
    """
    if value is None:
        raise ValueError(f"ERROR_INVALID_{field_name.upper()}")
    if not isinstance(value, list):
        raise ValueError(f"ERROR_INVALID_{field_name.upper()}")

    ints: List[int] = []
    seen = set()
    for v in value:
        if isinstance(v, bool) or isinstance(v, float) or isinstance(v, (dict, list)):
            raise ValueError(f"ERROR_INVALID_{field_name.upper()}")
        if isinstance(v, int):
            parsed = v
        elif isinstance(v, str):
            token = v.strip()
            if not token.isdigit():
                raise ValueError(f"ERROR_INVALID_{field_name.upper()}")
            parsed = int(token)
        else:
            raise ValueError(f"ERROR_INVALID_{field_name.upper()}")
        if parsed <= 0:
            raise ValueError(f"ERROR_INVALID_{field_name.upper()}")
        if parsed in seen:
            continue
        seen.add(parsed)
        ints.append(parsed)
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

# Schema for email input (legacy unrestricted shape — not used by PR31 /sendemail)
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


class InternalEmailPurpose(str, Enum):
    """Bounded purpose identifiers for PR31 internal /sendemail audit/rate buckets."""

    ADMIN_TEST = "ADMIN_TEST"
    OPERATIONS = "OPERATIONS"
    MIGRATION_COMPAT = "MIGRATION_COMPAT"


class InternalEmailSendRequest(BaseModel):
    """Restricted internal email request. No CC/BCC/attachments/HTML/from selection."""

    model_config = ConfigDict(extra="forbid")

    purpose: InternalEmailPurpose
    toAddress: EmailStr
    subject: str
    message: str


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
