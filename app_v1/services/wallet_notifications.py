"""PR49 wallet side effects run after commit (FastAPI background tasks).

Best effort: failures are logged and never undo or fail the wallet change.
Each helper owns its DB session (the request session is closed by then).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from ..models.user_table import User
from ..utils.common import FCMSend
from ..utils.email import send_email

logger = logging.getLogger(__name__)

WALLET_NOTIFICATION_TYPE = "wallet"
WALLET_NOTIFICATION_URL = "//My_Wallet"
DEFAULT_OPS_EMAIL = "openbidaccounts@wizzride.com"


def dispatch(background_tasks: Optional[BackgroundTasks], func: Callable[..., Any], *args: Any) -> None:
    """Queue ``func`` after the response, or run it now when there is no
    request context (scripts, tests calling CRUD directly)."""
    if background_tasks is not None:
        background_tasks.add_task(func, *args)
        return
    try:
        func(*args)
    except Exception as exc:
        logger.warning("WALLET_SIDE_EFFECT_FAILED category=%s", type(exc).__name__)


def rupees(paise: int) -> str:
    return f"\u20b9{paise // 100:,}.{paise % 100:02d}"


def notify_vendor_wallet(user_uid: int, title: str, body: str) -> None:
    """FCM push to the vendor. No navigation route; tap opens the app."""
    from ..database import SessionLocal
    from .notifications import send_notification_to_user

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.UID == int(user_uid)).first()
        user_app_id = str(getattr(user, "userAppId", "") or "").strip() if user else ""
        if not user_app_id:
            return
        result = send_notification_to_user(
            db,
            FCMSend(
                userAppId=user_app_id,
                title=title,
                body=body,
                url=WALLET_NOTIFICATION_URL,
                type=WALLET_NOTIFICATION_TYPE,
                soundFile="normal_notification",
            ),
        )
        message = getattr(result, "message", None)
        if message not in ("NOTIFICATION_SENT", "NO_TOKEN"):
            logger.warning("WALLET_PUSH_NOT_SENT uid=%s result=%s", user_uid, message)
    except Exception as exc:
        logger.warning("WALLET_PUSH_FAILED uid=%s category=%s", user_uid, type(exc).__name__)
    finally:
        db.close()


def ops_email_address() -> str:
    return (os.getenv("WALLET_OPS_EMAIL") or DEFAULT_OPS_EMAIL).strip()


def ops_email_from() -> str:
    return (os.getenv("WALLET_OPS_EMAIL_FROM") or "reservations@wizzride.com").strip()


def alert_ops_recharge_needs_review(
    order_id: str,
    user_uid: int,
    amount_paise: int,
    failure_code: str,
    environment: str,
) -> None:
    """Email ops when a recharge could not be credited automatically."""
    body = "\n".join(
        [
            "A wallet recharge could not be credited automatically and needs review.",
            "",
            f"Order ID        : {order_id}",
            f"Environment     : {environment}",
            f"Vendor UID      : {user_uid}",
            f"Expected amount : {rupees(int(amount_paise))}",
            f"Reason          : {failure_code}",
            "",
            "WHAT TO DO",
            "1. Open this order in the Cashfree dashboard and check its status and amount.",
            "2. If the vendor paid but the amount/currency differs, refund the payment from",
            "   Cashfree (the wallet was NOT credited).",
            "3. If the status is unusual, wait for Cashfree to settle it; the next status",
            "   check or webhook re-applies it automatically.",
            "No wallet balance was changed for this order.",
        ]
    )
    try:
        result = send_email(
            message=body,
            subject=f"[OpenBid] Wallet recharge needs review {order_id}",
            from_address=ops_email_from(),
            from_name="OpenBid Wallet",
            to_address=ops_email_address(),
            to_name="OpenBid Accounts",
            is_html=False,
        )
        if not (isinstance(result, dict) and result.get("message") == "SENT"):
            logger.error("WALLET_RECHARGE_ALERT_EMAIL_FAILED order=%s", order_id)
    except Exception:
        logger.error("WALLET_RECHARGE_ALERT_EMAIL_FAILED order=%s", order_id)
