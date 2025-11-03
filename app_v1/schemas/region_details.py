from pydantic import BaseModel
from typing import Optional, List


class RegionDetailBase(BaseModel):
    regionId : int
    regionName : str

    model_config={"from_attributes":True}

class RegionDetailCreate(RegionDetailBase):
    pass

class RegionDetailUpdate(BaseModel):
    regionName : Optional[str] = None


