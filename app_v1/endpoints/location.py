from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from ..crud.location import get_all_locations,get_all_cities,get_all_regions,get_region_city_selections
from ..schemas.location_details import Location,CityDetail,RegionDetail,LocationResponse
from ..schemas.city_list import CityListDetail
from ..schemas.region_details import RegionDetailBase
from ..utils.common import ErrorResponse
from ..auth.deps import get_current_user_id
from ..database import get_db
from typing import List,Union

router = APIRouter()


# @router.get("/getlocations", response_model=List[Location],openapi_extra={"security": [{"BearerAuth": [], "ClientIdHeader": []}]})
@router.get("/getlocations", response_model=Union[List[LocationResponse],ErrorResponse])
def read_locations(
    db:Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
    ):
    return get_all_locations(db)


@router.get("/getcities", response_model=List[CityListDetail])
def read_cities(db:Session = Depends(get_db),
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                ):
    return get_all_cities(db)


@router.get("/getregions",response_model=Union[List[RegionDetailBase],ErrorResponse])
def read_regions(db:Session = Depends(get_db)
                 ,user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                 ):
    return get_all_regions(db)

@router.get("/getuserregionpreferences",response_model=Union[List[RegionDetail],ErrorResponse])
def read_user_region_preferences(db:Session=Depends(get_db)
                                 ,user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                 userAppId:str = Query(...)):
    return get_region_city_selections(db,user_app_id=userAppId)