from pydantic import BaseModel
from typing import List, Optional

class CarType(BaseModel):
    car_type : str
    car_sub_type : str
    capacity : str
    image_url : str

    model_config = {"from_attributes": True}

class CarTypeList(BaseModel):
    car_types : List[CarType]

class CarTypeCreate(CarType):
    pass

class CarTypeUpdate(BaseModel):
    CTD : int
    car_type : Optional[str] = None
    car_sub_type : Optional[str] = None
    capacity : Optional[str] = None
    image_url : Optional[str] = None

class CarTypeDelete(BaseModel):
    CTD : int

class CarTypeResponse(CarType):
    model_config = {"from_attributes": True}


class CarTypeDetailResponse(BaseModel):
    CTD : int
    CARTYPE: str
    CARSUBTYPE:str
    CAPACITY:str
    IMAGEURL:str

    model_config = {"from_attributes": True}
