from pydantic import BaseModel
from typing import List,Optional


class CityListBase(BaseModel):
    cities : str
    state : str
    
    model_config={"from_attributes":True}

class CreateCityList(CityListBase):
    CLID : int

class UpdateCityLIst(BaseModel):
    CLID : int
    cities : Optional[str] = None
    state : Optional[str] = None

    model_config={"from_attributes":True}

class CityListDetail(BaseModel):
    CITYID : int
    CITY : str
    STATE : str

    model_config={"from_attributes":True}