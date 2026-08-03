"""PR27 — focused support identity resolver from AdminNumber + live userTable.

Shared-account model: exactly one AdminNumber row whose phone matches a live,
unlocked, non-tombstoned userTable row. Not a staff-role or multi-agent system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models.admin_number import AdminNumber
from ..models.user_table import User


@dataclass(frozen=True)
class SupportIdentity:
    """Minimal internal support identity. Never includes FCM token."""

    available: bool
    support_user_app_id: Optional[str] = None
    display_name: str = "OpenBid Support"
    profile_image_url: Optional[str] = None
    # True when zero/multiple rows or DB failure — notification path uses 503.
    configuration_invalid: bool = False


_DEFAULT_DISPLAY_NAME = "OpenBid Support"


def _is_tombstone_user_app_id(user_app_id: Optional[str]) -> bool:
    return ".DELETED" in str(user_app_id or "").upper()


def _normalize_phone(value: Optional[str]) -> str:
    return str(value or "").strip()


def resolve_support_identity(db: Session) -> SupportIdentity:
    """
    Resolve the single configured support user.

    Policy:
    - zero AdminNumber rows → unavailable
    - more than one row → unavailable / configuration_invalid
    - exactly one row → require matching live unlocked non-tombstoned user

    Does not close the request-scoped session. Does not log phones or rows.
    Does not return FCM tokens.
    """
    try:
        count = db.query(AdminNumber).count()
    except SQLAlchemyError:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    if count == 0:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    if count > 1:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    try:
        record = db.query(AdminNumber).first()
    except SQLAlchemyError:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    phone = _normalize_phone(getattr(record, "phonenumber", None) if record else None)
    if not phone:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    try:
        user = db.query(User).filter(User.userAppId == phone).first()
    except SQLAlchemyError:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    if user is None:
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    user_app_id = _normalize_phone(getattr(user, "userAppId", None))
    if not user_app_id or _is_tombstone_user_app_id(user_app_id):
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    if bool(getattr(user, "lockApp", False)):
        return SupportIdentity(
            available=False,
            display_name=_DEFAULT_DISPLAY_NAME,
            configuration_invalid=True,
        )

    profile_pic = getattr(user, "profilePicture", None)
    profile_image_url = ""
    if profile_pic is not None:
        profile_image_url = str(profile_pic).strip()

    return SupportIdentity(
        available=True,
        support_user_app_id=user_app_id,
        display_name=_DEFAULT_DISPLAY_NAME,
        profile_image_url=profile_image_url,
        configuration_invalid=False,
    )
