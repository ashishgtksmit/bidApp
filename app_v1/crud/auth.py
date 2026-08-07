"""Auth CRUD — login, refresh, insert, password reset (PR37 + PR38)."""

from __future__ import annotations

import hashlib
import os
from datetime import date, datetime
from typing import Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..auth.deps import (
    record_legacy_refresh_converted,
    resolve_token_user,
)
from ..auth.jwt import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    IDENTITY_VERSION_IMMUTABLE,
    IDENTITY_VERSION_LEGACY_PHONE,
    create_access_token,
    create_refresh_token,
    decode_token,
    identity_version_to_mint,
)
from ..models.user_table import User, _new_account_session_id, _new_auth_subject_id
from ..schemas.user_table import (
    LoginResponseWithTokens,
    TokenPair,
    UserCreate,
    UserLogin,
)
from ..utils.common import EmailErrorResponse
from ..utils.rate_limit import enforce_rate_limit
from ..utils.security import verify_and_update_password


def _is_tombstone_app_id(user_app_id: str | None) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()


def _refresh_fingerprint(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:32]


def _raise_refresh(status_code: int, detail: str) -> None:
    raise HTTPException(status_code=status_code, detail=detail)


def _roles_for_user(user: User) -> list[str]:
    return ["vendor"] if bool(getattr(user, "alsoVendor", False)) else ["user"]


def ensure_auth_subject_id(user: User) -> str:
    """Return existing opaque authSubjectId, generating one if missing (pre-migration)."""
    existing = str(getattr(user, "authSubjectId", "") or "").strip()
    if existing:
        return existing
    user.authSubjectId = _new_auth_subject_id()
    return str(user.authSubjectId)


def _mint_subject_for_user(user: User) -> Tuple[str, int]:
    """Return (auth_subject, identity_version) for newly minted tokens.

    Version 2 → opaque authSubjectId.
    Version 1 (emergency mint rollback only) → phone userAppId.
    """
    version = identity_version_to_mint()
    if version == IDENTITY_VERSION_LEGACY_PHONE:
        return str(user.userAppId), IDENTITY_VERSION_LEGACY_PHONE
    auth_subject = ensure_auth_subject_id(user)
    return auth_subject, IDENTITY_VERSION_IMMUTABLE


def _mint_token_pair(
    db: Session,
    user: User,
    *,
    client_id: Optional[str],
) -> TokenPair:
    roles = _roles_for_user(user)
    session_version = int(user.sessionVersion)
    account_session_id = str(user.accountSessionId)
    auth_subject, identity_version = _mint_subject_for_user(user)
    access_token = create_access_token(
        db=db,
        auth_subject=auth_subject,
        identity_version=identity_version,
        session_version=session_version,
        account_session_id=account_session_id,
        client_id=client_id,
        roles=roles,
    )
    refresh_token = create_refresh_token(
        db=db,
        auth_subject=auth_subject,
        identity_version=identity_version,
        session_version=session_version,
        account_session_id=account_session_id,
        client_id=client_id,
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def login_user_auth(
    db: Session,
    login_data: UserLogin,
    client_id: Optional[str],
):
    try:
        user = (
            db.query(User)
            .filter(User.userAppId == login_data.userAppId)
            .first()
        )

        if not user:
            return EmailErrorResponse(message="NOT REGISTERED")

        if _is_tombstone_app_id(user.userAppId):
            return EmailErrorResponse(message="NOT REGISTERED")

        # 1) Verify password (supports bcrypt_sha256 and bcrypt)
        ok, new_hash = verify_and_update_password(
            login_data.password, user.password
        )
        if not ok:
            return EmailErrorResponse(message="USERNAME OR PASSWORD WRONG")

        # 2) If Passlib suggests an upgrade, persist it (does NOT bump sessionVersion)
        if new_hash:
            user.password = new_hash
            db.flush()

        user_app_id = user.userAppId
        also_vendor = bool(user.alsoVendor)

        user_dict = {
            "FULLNAME": user.fullName,
            "EMAIL": user.emailId,
            "APPID": user_app_id,
            "DOB": user.dob,
            "CITY": user.city,
            "GENDER": user.gender,
            "ALTERNATENUM": user.alternateNumber,
            "PROFILEPIC": user.profilePicture,
            "VENDOR": also_vendor,
            "CUSTOMERRATING": user.customerRating,
            "TOTALCUSTOMERRATING": user.totalCustomerReviews,
        }
        if also_vendor:
            user_dict.update(
                {
                    "VENDORRATING": float(user.rating)
                    if user.rating is not None
                    else None,
                    "TOTALVENDORRATING": user.totalNoOfReviews,
                }
            )

        login_status = "LOGGEDIN"
        message = (
            "LOGIN SUCCESS"
            if user.user_login_status != "LOGGEDIN"
            else "ALREADY_LOGGEDIN"
        )

        user.user_login_status = login_status
        # Only persist a non-empty FCM token from login. Omitting/null/blank
        # must not clear a previously synced token (Flutter often omits the
        # field when getToken has not returned yet).
        incoming_fcm = (login_data.fcmToken or "").strip()
        if incoming_fcm:
            user.fcmToken = incoming_fcm
        ensure_auth_subject_id(user)
        user.tableTimestamp = func.current_timestamp()
        db.commit()

        pair = _mint_token_pair(db, user, client_id=client_id)

        return LoginResponseWithTokens(
            message=message,
            user=[user_dict],
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            token_type="bearer",
            expires_in=pair.expires_in,
        )

    except SQLAlchemyError as e:
        db.rollback()
        print(str(e))
        return EmailErrorResponse(message="LOGIN_FAILED")
    finally:
        db.close()


def refresh_tokens(
    db: Session,
    refresh_token: str,
    client_id: Optional[str],
    *,
    client_ip: Optional[str] = None,
):
    """Validate refresh token only; mint a new token pair (PR37 + PR38).

    Raises HTTPException with hard statuses (no soft-200 auth errors).
    Does not require or validate an access JWT.
    Valid PR37 phone-sub refresh converts to a version-2 pair when minting v2.
    """
    if not refresh_token or not str(refresh_token).strip():
        _raise_refresh(
            status.HTTP_401_UNAUTHORIZED, "INVALID_REFRESH_TOKEN"
        )

    token_str = str(refresh_token).strip()
    ip_bucket = client_ip or "unknown"
    fp = _refresh_fingerprint(token_str)

    def _invalid_rate_limit() -> None:
        limited = enforce_rate_limit(
            db,
            bucket_key=f"refresh:invalid:fp:{fp}",
            max_hits=int(
                os.getenv("RATE_LIMIT_REFRESH_INVALID_PER_BUCKET", "10")
            ),
            window_seconds=int(
                os.getenv("RATE_LIMIT_REFRESH_INVALID_WINDOW_SECONDS", "900")
            ),
            fail_closed=True,
        )
        if limited is not None:
            _raise_refresh(
                status.HTTP_429_TOO_MANY_REQUESTS, "REFRESH_RATE_LIMITED"
            )
        limited_ip = enforce_rate_limit(
            db,
            bucket_key=f"refresh:invalid:ip:{ip_bucket}",
            max_hits=int(
                os.getenv("RATE_LIMIT_REFRESH_INVALID_PER_BUCKET", "10")
            ),
            window_seconds=int(
                os.getenv("RATE_LIMIT_REFRESH_INVALID_WINDOW_SECONDS", "900")
            ),
            fail_closed=True,
        )
        if limited_ip is not None:
            _raise_refresh(
                status.HTTP_429_TOO_MANY_REQUESTS, "REFRESH_RATE_LIMITED"
            )

    try:
        try:
            payload = decode_token(
                db=db, token=token_str, client_id=client_id
            )
        except ValueError as exc:
            _invalid_rate_limit()
            msg = str(exc).lower()
            if "expired" in msg:
                _raise_refresh(
                    status.HTTP_401_UNAUTHORIZED, "REFRESH_TOKEN_EXPIRED"
                )
            _raise_refresh(
                status.HTTP_401_UNAUTHORIZED, "INVALID_REFRESH_TOKEN"
            )

        if payload.get("type") != "refresh":
            _invalid_rate_limit()
            _raise_refresh(
                status.HTTP_401_UNAUTHORIZED, "INVALID_REFRESH_TOKEN"
            )

        # Pre-PR37 claimless / missing subject → reject before identity resolve
        if (
            not payload.get("sub")
            or payload.get("session_version") is None
            or not payload.get("session_id")
        ):
            _invalid_rate_limit()
            _raise_refresh(
                status.HTTP_401_UNAUTHORIZED, "INVALID_REFRESH_TOKEN"
            )

        # Reject identity-version downgrade attempts (v2 token claiming v1)
        raw_iv = payload.get("identity_version")
        if raw_iv is not None:
            try:
                claimed_iv = int(raw_iv)
            except (TypeError, ValueError):
                _invalid_rate_limit()
                _raise_refresh(status.HTTP_401_UNAUTHORIZED, "SESSION_INVALID")
            if claimed_iv not in (
                IDENTITY_VERSION_LEGACY_PHONE,
                IDENTITY_VERSION_IMMUTABLE,
            ):
                _invalid_rate_limit()
                _raise_refresh(status.HTTP_401_UNAUTHORIZED, "SESSION_INVALID")

        try:
            authenticated = resolve_token_user(
                db,
                payload,
                expected_type="refresh",
                allow_legacy=None,
                raise_session_invalid=True,
            )
        except HTTPException as exc:
            detail = str(getattr(exc, "detail", "") or "")
            if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                _invalid_rate_limit()
                if detail == "SESSION_INVALID":
                    _raise_refresh(
                        status.HTTP_401_UNAUTHORIZED, "SESSION_INVALID"
                    )
                _raise_refresh(
                    status.HTTP_401_UNAUTHORIZED, "SESSION_INVALID"
                )
            raise

        user = db.query(User).filter(User.UID == authenticated.uid).first()
        if user is None or _is_tombstone_app_id(user.userAppId):
            _invalid_rate_limit()
            _raise_refresh(status.HTTP_401_UNAUTHORIZED, "SESSION_INVALID")

        # lockApp: allowed for live users (PR37)
        limited = enforce_rate_limit(
            db,
            bucket_key=f"refresh:valid:sid:{user.accountSessionId}",
            max_hits=int(
                os.getenv("RATE_LIMIT_REFRESH_VALID_PER_SESSION", "20")
            ),
            window_seconds=int(
                os.getenv("RATE_LIMIT_REFRESH_VALID_WINDOW_SECONDS", "3600")
            ),
            fail_closed=True,
        )
        if limited is not None:
            _raise_refresh(
                status.HTTP_429_TOO_MANY_REQUESTS, "REFRESH_RATE_LIMITED"
            )

        ensure_auth_subject_id(user)
        db.flush()

        if authenticated.identity_version == IDENTITY_VERSION_LEGACY_PHONE:
            record_legacy_refresh_converted()

        return _mint_token_pair(db, user, client_id=client_id)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        _raise_refresh(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "REFRESH_FAILED"
        )
    except Exception:
        db.rollback()
        _raise_refresh(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "REFRESH_FAILED"
        )


def insert_user(db: Session, user_data: UserCreate):
    try:
        with db.begin():
            existing_user = (
                db.query(User)
                .filter(User.userAppId == user_data.userAppId)
                .first()
            )
            if existing_user:
                return EmailErrorResponse(message="USER ALREADY PRESENT")

            dob = user_data.dob if user_data.dob else date.today()
            gender = (
                user_data.gender
                if user_data.gender and user_data.gender.strip() != ""
                else "Male"
            )
            joining_date = date.today()
            new_user = User(
                userAppId=user_data.userAppId,
                password=user_data.password,
                alternateNumber=user_data.alternateNumber,
                fullName=user_data.fullName,
                dob=dob,
                city=user_data.city,
                gender=gender,
                custSignUpDate=joining_date,
                emailId=user_data.emailId,
                rating=user_data.rating,
                totalNoOfReviews=user_data.totalCustomerRevies,
                alsoVendor=False,
                vendorApproved=False,
                lockApp=False,
                sessionVersion=1,
                accountSessionId=_new_account_session_id(),
                authSubjectId=_new_auth_subject_id(),
                tableTimestamp=datetime.now(),
            )

            db.add(new_user)
            db.commit()

            return EmailErrorResponse(message="INSERTED")

    except SQLAlchemyError as e:
        print(str(e))
        db.rollback()
        return EmailErrorResponse(message="ERROR")
    finally:
        db.close()


def update_password(
    db: Session,
    user_app_id: str,
    password: str,
    reset_token: str,
):
    """Update password + increment sessionVersion in the same transaction."""
    from ..utils.otp import consume_reset_token

    try:
        user_app_id = str(user_app_id).strip() if user_app_id is not None else ""
        if not user_app_id:
            return EmailErrorResponse(message="FAILED")

        token_error = consume_reset_token(
            db,
            user_app_id=user_app_id,
            reset_token=reset_token,
        )
        if token_error is not None:
            return EmailErrorResponse(message=token_error)

        user = (
            db.query(User)
            .filter(User.userAppId == user_app_id)
            .with_for_update()
            .first()
        )
        if user is None or _is_tombstone_app_id(user.userAppId):
            return EmailErrorResponse(message="FAILED")

        current_sv = int(user.sessionVersion or 1)
        user.password = password
        user.sessionVersion = current_sv + 1
        db.commit()
        return EmailErrorResponse(message="UPDATED")
    except SQLAlchemyError:
        db.rollback()
        return EmailErrorResponse(message="ERROR")
    finally:
        db.close()
