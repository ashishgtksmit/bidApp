from pydantic import BaseModel,Field
from typing import Optional
from datetime import date, datetime
from ..utils.common import UppercaseBase

class VendorCarTypeBase(BaseModel):
    manufacturer : str
    model : str    
    variant : str
    year : str
    fuelType : str
    seatingCapacity : int
    CTD : int

    model_config={"from_attributes":True}

class InsertVendorCarType(VendorCarTypeBase):
    pass

class UpdateVendorCarType(BaseModel):    
    manufacturer : Optional[str] = None
    model : Optional[str] = None
    variant : Optional[str] = None
    variant : Optional[str] = None
    year : Optional[str] = None
    fuelType : Optional[str] = None
    seatingCapacity : Optional[int] = None
    CTD : Optional[int] = None

    model_config={"from_attributes":True}


class DeleteVendorCarType(UppercaseBase):
    vcrtid : int
    

class VendorCarTypeDetail(VendorCarTypeBase, UppercaseBase):
    vcrtid : int     
    car_type : Optional[str] = None
    car_Sub_Type : Optional[str] = None
    capacity : Optional[str] = None
    image_Url : Optional[str] = None                        

    


