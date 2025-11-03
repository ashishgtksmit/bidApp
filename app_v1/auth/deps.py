from fastapi import Depends, Header, HTTPException, status, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from ..database import get_db
from sqlalchemy.orm import Session
from ..auth.jwt import decode_token

# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
# def get_current_user_id(
#         token : str = Depends(oauth2_scheme),
#         db:Session = Depends(get_db),
#         x_client_id : str | None = Header(default=None, alias="X-Client-Id"),
# )->str :
#     try:
#         payload = decode_token(db=db,token=token,client_id=x_client_id)
#         if payload.get("type") != "access":
#             raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="Invalid Token Type")
#         return payload["sub"]
#     except Exception:
#         raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="Could not validate credentials")

http_bearer = HTTPBearer(auto_error=True, scheme_name="BearerAuth")                  # <- HTTP Bearer for JWT
client_id_scheme = APIKeyHeader(name="X-Client-Id", auto_error=False, scheme_name="ClientIdHeader")  # optional header

def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Security(http_bearer),
    db: Session = Depends(get_db),
    x_client_id: str | None = Security(client_id_scheme),
) -> str:
    try:
        token = credentials.credentials  # the raw JWT
        print(token)
        payload = decode_token(db=db, token=token, client_id=x_client_id)
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Token Type")
        return payload["sub"]
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")