from pydantic import BaseModel
from typing import Optional

class RequestTypeBase(BaseModel):
    RTDID : int
    requestType : str

    model_config={"from_attributes":True}

    