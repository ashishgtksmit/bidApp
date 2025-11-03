# project_root/api/endpoints/auth.py
from fastapi import APIRouter, Depends, Header
from typing import Optional, Union
from ..schemas.user_table import UserLogin,LoginResponseWithTokens, TokenPair, RefreshRequest
from ..utils.common import ErrorResponse
from ..database import get_db
from sqlalchemy.orm import Session
from ..crud.auth import login_user_auth, refresh_tokens
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.post("/login", response_model=Union[LoginResponseWithTokens, ErrorResponse])
def login_user_endpoint(
    login_data: UserLogin,
    db: Session = Depends(get_db),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    return login_user_auth(db=db, login_data=login_data, client_id=x_client_id)

@router.post("/refresh", response_model=Union[TokenPair, ErrorResponse])
def refresh_token_endpoint(
    data: RefreshRequest,
    db: Session = Depends(get_db)
    ,user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    return refresh_tokens(db=db, refresh_token=data.refresh_token, client_id=x_client_id)