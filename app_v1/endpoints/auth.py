# project_root/api/endpoints/auth.py
from fastapi import APIRouter, Depends, Header, Query, HTTPException, Request, status
from typing import Optional, Union
import os
from ..schemas.user_table import (
    UserLogin,
    LoginResponseWithTokens,
    TokenPair,
    RefreshRequest,
    UserCreate,
    WsAuthRequest,
    WsAuthResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
)
from ..models.user_table import User
from ..utils.common import ErrorResponse
from ..database import get_db
from sqlalchemy.orm import Session
from ..crud.auth import login_user_auth, refresh_tokens, insert_user, update_password
from ..auth.deps import validate_access_session
from ..auth.jwt import decode_token
from ..utils.otp import verify_otp_for_user
from ..utils.rate_limit import client_ip_from_request, enforce_rate_limit


router = APIRouter()

@router.post("/insertuser",response_model=ErrorResponse)
def create_user(user_data:UserCreate, db:Session=Depends(get_db),
                # AuthenticatedUser gate intentionally not applied (public registration).
                ):
    return insert_user(db,user_data)

@router.post("/login", response_model=Union[LoginResponseWithTokens, ErrorResponse])
def login_user_endpoint(
    login_data: UserLogin,
    db: Session = Depends(get_db),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    return login_user_auth(db=db, login_data=login_data, client_id=x_client_id)


@router.post(
    "/verifyotp",
    response_model=Union[OtpVerifyResponse, ErrorResponse],
)
def verify_otp_endpoint(
    request: Request,
    body: OtpVerifyRequest,
    db: Session = Depends(get_db),
):
    """
    Public OTP verification (PR5).

    On success returns OTP_VERIFIED + short-lived one-time reset_token.
    Never returns the OTP itself.
    """
    limited = enforce_rate_limit(
        db,
        bucket_key=f"verifyotp:ip:{client_ip_from_request(request)}",
        max_hits=int(os.getenv("RATE_LIMIT_VERIFYOTP_PER_IP", "60")),
        window_seconds=int(os.getenv("RATE_LIMIT_VERIFYOTP_WINDOW_SECONDS", "900")),
    )
    if limited is not None:
        return limited
    limited = enforce_rate_limit(
        db,
        bucket_key=f"verifyotp:user:{body.userAppId}",
        max_hits=int(os.getenv("RATE_LIMIT_VERIFYOTP_PER_APPID", "20")),
        window_seconds=int(os.getenv("RATE_LIMIT_VERIFYOTP_WINDOW_SECONDS", "900")),
    )
    if limited is not None:
        return limited
    return verify_otp_for_user(db, user_app_id=body.userAppId, otp=body.otp)


@router.put("/updatepassword", response_model=ErrorResponse)
def user_update_password(
    db: Session = Depends(get_db),
    userAppId: str = Query(...),
    password: str = Query(...),
    resetToken: str = Query(..., description="One-time token from POST /verifyotp"),
):
    """
    Password reset requires a valid resetToken issued by POST /verifyotp.
    Direct API callers without OTP proof are rejected.
    """
    return update_password(
        db,
        user_app_id=userAppId,
        password=password,
        reset_token=resetToken,
    )

@router.post(
    "/refresh",
    response_model=TokenPair,
    # PR37: refresh_token body is the only credential. Authorization may be
    # present from older clients but is never required or validated here.
)
def refresh_token_endpoint(
    request: Request,
    data: RefreshRequest,
    db: Session = Depends(get_db),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    """Mint a new token pair from a valid refresh token (no access JWT gate)."""
    return refresh_tokens(
        db=db,
        refresh_token=data.refresh_token,
        client_id=x_client_id,
        client_ip=client_ip_from_request(request),
    )

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

    # 1) Decode/validate JWT + PR38 identity resolve (returns AuthenticatedUser)
    try:
        payload = decode_token(
            db=db,
            token=body.token,
            client_id=body.client_id,
            verify_aud=True,
        )
        authenticated = validate_access_session(db, payload=payload)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token missing 'exp'",
        )

    # 2) Load user from DB by UID (phone appid returned to worker unchanged)
    user = (
        db.query(User)
        .filter(User.UID == authenticated.uid)
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
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



