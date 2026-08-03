from sqlalchemy.orm import Session
from datetime import datetime,timedelta,timezone
import os
from ..models.client_secrets import ClientSecret
from typing import Optional,Dict,Any
from jose import jwt,JWTError

def _now():
    return datetime.now(timezone.utc)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
    
# env
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 15)
REFRESH_TOKEN_EXPIRE_DAYS = _env_int("REFRESH_TOKEN_EXPIRE_DAYS", 30)
JWT_ISSUER = os.getenv("JWT_ISSUER")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE")

def get_signing_secret(db: Session, client_id : Optional[str]) -> str:
    if not client_id:
        return JWT_SECRET
    cs = db.query(ClientSecret).filter(
            ClientSecret.clientId == client_id,
            ClientSecret.isActive==True
        ).first()
    
    return cs.secretKey if cs and cs.secretKey else JWT_SECRET

def create_token(*,db:Session, 
                 subject : str, #userAppId
                 token_type : str, #access|refresh
                 client_id : Optional[str],
                 extra_claims : Optional[Dict[str, Any]] = None,                 
                 ) -> str :
    assert token_type in ("access","refresh")
    now = _now()
    exp = (now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)) if token_type =="access"\
            else (now + timedelta(minutes=REFRESH_TOKEN_EXPIRE_DAYS))
    payload = {
        "sub":subject,
        "type":token_type,
        "iat":int(now.timestamp()),
        "nbf":int(now.timestamp()),
        "exp":int(exp.timestamp()),
        "iss":JWT_ISSUER,
        "aud":JWT_AUDIENCE.strip()
    }

    if extra_claims:
        payload.update(extra_claims)
    secret = get_signing_secret(db,client_id)
    return jwt.encode(payload,secret,algorithm=JWT_ALGORITHM)

def decode_token(
        *,
        db:Session,
        token:str,
        client_id:Optional[str],
        verify_aud:bool = True
) -> Dict[str,Any]:
    secret = get_signing_secret(db,client_id)
    aud = JWT_AUDIENCE.strip() if JWT_AUDIENCE else None   # defensively strip
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=aud if verify_aud else None,
            issuer=JWT_ISSUER,
            options={"verify_aud":verify_aud},
        )
    except JWTError as e:
        raise ValueError(str(e))
        
    
