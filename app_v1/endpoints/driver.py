from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Union,List
from ..utils.common import EmailErrorResponse, ErrorResponse
from ..schemas.driver_details import UpdateDriverDetail,DeleteDriverDetail,CreateDriverDetail,DriverDetailResponse
from ..database import get_db
from ..crud.driver import update_driver_details,delete_driver_by_id,insert_driver,get_all_driver_for_vendor
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.post("/updatedriverdetails",response_model=Union[EmailErrorResponse, ErrorResponse])
def driver_details_update(driver_data : UpdateDriverDetail, 
                          user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                          db:Session = Depends(get_db)):
    return update_driver_details(db,driver_data)

@router.put("/deletedriverfromprofile",response_model=Union[EmailErrorResponse,ErrorResponse])
def delete_driver(driver_data : DeleteDriverDetail, 
                  user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                  db: Session = Depends(get_db)):
    return delete_driver_by_id(db,driver_data)

@router.post("/insertnewdriver",response_model=EmailErrorResponse)
def create_new_driver(driver_data : CreateDriverDetail, 
                      user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                      db : Session = Depends(get_db)):
    return insert_driver(db,driver_data)

@router.get("/viewdriversforvendor", response_model=Union[List[DriverDetailResponse],ErrorResponse])
def read_all_drivers_for_vendors(db:Session = Depends(get_db), 
                                 user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                                 userAppId : str = Query(...)):
    return get_all_driver_for_vendor(db,userappid=userAppId)
