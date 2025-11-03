from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from ..schemas.user_table import UserLogin,LoginResponseWithTokens,TokenPair
from ..utils.common import EmailErrorResponse
from ..models.user_table import User
from ..utils.security import verify_and_update_password
from ..auth.jwt import create_token,decode_token,ACCESS_TOKEN_EXPIRE_MINUTES
from typing import Optional

# app_v1/crud/auth.py  (your file, patched)

from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from ..schemas.user_table import UserLogin
from ..schemas.user_table import LoginResponseWithTokens, TokenPair
from ..utils.common import EmailErrorResponse
from ..models.user_table import User
from ..utils.security import verify_and_update_password
from ..auth.jwt import create_token, decode_token, ACCESS_TOKEN_EXPIRE_MINUTES
from typing import Optional

def login_user_auth(
        db: Session,
        login_data: UserLogin,
        client_id: Optional[str],
):
    try:
        # with db.begin():
            user = db.query(
                User.userAppId,
                User.password,
                User.alternateNumber,
                User.fullName,
                User.emailId,
                User.dob,
                User.city,
                User.gender,
                User.profilePicture,
                User.user_login_status,
                User.alsoVendor,
                User.rating,
                User.customerRating,
                User.totalNoOfReviews,
                User.totalCustomerReviews
            ).filter(User.userAppId == login_data.userAppId).first()

            if not user:
                return EmailErrorResponse(message="NOT REGISTERED")

            (
                user_app_id,
                stored_password,
                alternate_number,
                full_name,
                email_id,
                dob,
                city,
                gender,
                profile_picture,
                user_login_status,
                also_vendor,
                rating,
                customer_rating,
                total_vendor_rating,
                total_customer_rating
            ) = user

            # 1) Verify password (supports bcrypt_sha256 and bcrypt)
            ok, new_hash = verify_and_update_password(login_data.password, stored_password)
            if not ok:
                return EmailErrorResponse(message="USERNAME OR PASSWORD WRONG")

            # 2) If Passlib suggests an upgrade, persist it
            if new_hash:
                db.query(User).filter(User.userAppId == user_app_id).update({User.password: new_hash})
                db.flush()

            # 3) Build your original user payload
            user_dict = {
                "FULLNAME": full_name,
                "EMAIL": email_id,
                "APPID": user_app_id,
                "DOB": dob,
                "CITY": city,
                "GENDER": gender,
                "ALTERNATENUM": alternate_number,
                "PROFILEPIC": profile_picture,
                "VENDOR": also_vendor,
                "CUSTOMERRATING": customer_rating,
                "TOTALCUSTOMERRATING": total_customer_rating
            }
            if also_vendor:
                user_dict.update({
                    "VENDORRATING": float(rating) if rating is not None else None,
                    "TOTALVENDORRATING": total_vendor_rating
                })

            status = "LOGGEDIN"
            message = "LOGIN SUCCESS" if user_login_status != "LOGGEDIN" else "ALREADY_LOGGEDIN"

            # 4) ✅ DO NOT compare with plaintext password here
            updated = (
                db.query(User)
                .filter(User.userAppId == user_app_id)
                .update({
                    User.user_login_status: status,
                    User.fcmToken: login_data.fcmToken,
                    User.tableTimestamp: func.current_timestamp()
                })
            )
            db.commit()
            if updated == 0:
                # This would be unusual now; keep as a safety
                return EmailErrorResponse(message="LOGIN_FAILED")

            # 5) Create tokens (typo fix: extra_claims)
            roles = ["vendor"] if also_vendor else ["user"]
            access_token = create_token(
                db=db,
                subject=user_app_id,
                token_type="access",
                client_id=client_id,
                extra_claims={"roles": roles}   # <-- fixed name
            )
            refresh_token = create_token(
                db=db,
                subject=user_app_id,
                token_type="refresh",
                client_id=client_id
            )

            return LoginResponseWithTokens(
                message=message,
                user=[user_dict],
                access_token=access_token,
                refresh_token=refresh_token,
                token_type="bearer",
                expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60  # seconds
            )

    except SQLAlchemyError as e:
        db.rollback()        
        print(str(e))
        return EmailErrorResponse(message="LOGIN_FAILED")
    finally:
        db.close()

def refresh_tokens(
        db:Session,
        refresh_token : str,
        client_id : Optional[str]
):
    try:
        payload = decode_token(db=db,token=refresh_token,client_id=client_id)
        if payload.get("type") != "refresh":
            return EmailErrorResponse(message="INVALID_TOKEN_TYPE")
        
        user_app_id = payload.get("sub")
        if not user_app_id:
            return EmailErrorResponse(message="INVALID_TOKEN")
        
        exists = db.query(User).filter(User.userAppId == user_app_id).first()
        if not exists:
            return EmailErrorResponse(message="USER_NOT_FOUND")
        
        new_access = create_token(
            db=db,
            subject=user_app_id,
            token_type="access",
            client_id=client_id
        )
        new_refresh = create_token(
            db=db,
            subject=user_app_id,
            token_type="refresh",
            client_id=client_id
        )

        return TokenPair(
            access_token=new_access,
            refresh_token=new_refresh,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES*60,
        )
    except Exception:
        db.rollback()
        return EmailErrorResponse(message="INVALID_REFRESH_TOKEN")
    finally:
        db.close()