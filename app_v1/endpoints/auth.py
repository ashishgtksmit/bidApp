# project_root/api/endpoints/auth.py
from fastapi import APIRouter, Depends, Header, Query, HTTPException, status
from typing import Optional, Union
from ..schemas.user_table import (UserLogin,LoginResponseWithTokens, TokenPair, 
                                  RefreshRequest, UserCreate, WsAuthRequest, WsAuthResponse)
from ..models.user_table import User
from ..utils.common import ErrorResponse
from ..database import get_db
from sqlalchemy.orm import Session
from ..crud.auth import login_user_auth, refresh_tokens, insert_user, update_password
from ..auth.deps import get_current_user_id
from ..auth.jwt import decode_token


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

# --------------------------------------------------
# NEW: strict WebSocket auth + exp for TTL
# --------------------------------------------------

@router.post("/ws-validate", response_model=WsAuthResponse)
def ws_validate_for_websocket(
    body: WsAuthRequest,
    db: Session = Depends(get_db),
) -> WsAuthResponse:
    """
    Endpoint used ONLY by the WebSocket service.

    - Validates the access token (same rules as HTTP)
    - Confirms the user exists
    - Enforces Vendor/Customer rules
    - Returns appid + normalized flag + token exp (unix ts)
    """

    # 1) Decode/validate JWT
    try:
        payload = decode_token(
            db=db,
            token=body.token,
            client_id=body.client_id,
            verify_aud=True,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user_app_id = payload.get("sub")
    if not user_app_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token missing 'sub'",
        )

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token missing 'exp'",
        )

    # 2) Load user from DB
    user = (
        db.query(User)
        .filter(User.userAppId == user_app_id)
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # 3) Normalize and validate flag
    flag = body.flag.strip()
    lf = flag.lower()
    if lf.startswith("cust"):
        flag = "Customer"
    elif lf.startswith("vend"):
        flag = "Vendor"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid flag",
        )

    # Optional strict vendor gating: only allow Vendor if user is a vendor & approved
    if flag == "Vendor":
        if not (user.alsoVendor and user.vendorApproved):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User not allowed to subscribe as vendor",
            )

    # 4) Return appid from DB (not from client)
    return WsAuthResponse(
        appid=user.userAppId,
        flag=flag,
        exp=exp,
    )



