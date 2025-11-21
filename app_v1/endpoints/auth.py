# project_root/api/endpoints/auth.py
from fastapi import APIRouter, Depends, Header, Query
from typing import Optional, Union
from ..schemas.user_table import UserLogin,LoginResponseWithTokens, TokenPair, RefreshRequest, UserCreate
from ..utils.common import ErrorResponse
from ..database import get_db
from sqlalchemy.orm import Session
from ..crud.auth import login_user_auth, refresh_tokens, insert_user, update_password
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.post("/insertuser",response_model=ErrorResponse)
def create_user(user_data:UserCreate, db:Session=Depends(get_db),
                # user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                ):
    return insert_user(db,user_data)

@router.post("/login", response_model=Union[LoginResponseWithTokens, ErrorResponse])
def login_user_endpoint(
    login_data: UserLogin,
    db: Session = Depends(get_db),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    return login_user_auth(db=db, login_data=login_data, client_id=x_client_id)

@router.put("/updatepassword",response_model=ErrorResponse)
def user_update_password(db:Session=Depends(get_db), 
                        #  user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                         userAppId : int = Query(...), password : str = Query(...)):
    return update_password(db,user_app_id=userAppId,password=password)

@router.post("/refresh", response_model=Union[TokenPair, ErrorResponse])
def refresh_token_endpoint(
    data: RefreshRequest,
    db: Session = Depends(get_db)
    ,user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    return refresh_tokens(db=db, refresh_token=data.refresh_token, client_id=x_client_id)



