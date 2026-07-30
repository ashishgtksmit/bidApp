from pydantic import BaseModel
from typing import List, Optional

class Location(BaseModel):
    LID : int
    location : str    
    location_shortCode : Optional[str] = None
    regionId : int

    model_config = {"from_attributes": True}

class LocationList(BaseModel):
    location : List[Location]


# for Creating a New Location
class LocationCreate(Location):
    pass

# For Updating an Existing Location
class LocationUpdate(BaseModel):
    location : Optional[str] = None    
    location_shortCode : Optional[str] = None

# For Resposne 
class LocationResponse(Location):
    LID : int

    model_config = {"from_attributes": True}

class CityDetail(BaseModel):
    LID : int
    CITY : str
    SHORT : Optional[str] = None
    SELECTED : bool

    model_config={"from_attributes":True}

class RegionDetail(BaseModel):
    REGION_ID : int
    REGION_NAME : str
    SELECTED : bool
    CITIES : List[CityDetail]

    model_config={"from_attributes":True}


class LocationResponse(BaseModel):
    LOCATIONCODE: int
    LOCATION: str
    LOCATIONSHORTCODE: Optional[str] = None
    REGIONID: int
    REGIONNAME: Optional[str] = None