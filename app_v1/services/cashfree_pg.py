"""PR48 Cashfree Payment Gateway client for wallet recharge.

Configuration comes only from environment / Azure App Settings:
  CASHFREE_ENV               SANDBOX | PRODUCTION
  CASHFREE_CLIENT_ID
  CASHFREE_CLIENT_SECRET
  CASHFREE_API_VERSION       default 2022-09-01
  CASHFREE_WALLET_NOTIFY_URL https URL of POST /webhooks/cashfree/wallet

Never logs credentials, payment session ids, or customer PII.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

ENV_SANDBOX = "SANDBOX"
ENV_PRODUCTION = "PRODUCTION"
_BASE_URLS = {
    ENV_SANDBOX: "https://sandbox.cashfree.com",
    ENV_PRODUCTION: "https://api.cashfree.com",
}
DEFAULT_API_VERSION = "2022-09-01"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class CashfreeConfig:
    environment: str
    client_id: str
    client_secret: str
    api_version: str
    notify_url: str

    @property
    def base_url(self) -> str:
        return _BASE_URLS[self.environment]


class CashfreeNotConfigured(Exception):
    pass


class CashfreeError(Exception):
    """Gateway call failed.

    ``definite`` is True only for a 4xx response, meaning Cashfree rejected
    the request. Network errors, timeouts, 5xx and bad JSON are ambiguous.
    """

    def __init__(self, kind: str, *, status_code: Optional[int] = None, definite: bool = False):
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code
        self.definite = definite


def load_cashfree_config() -> CashfreeConfig:
    environment = (os.getenv("CASHFREE_ENV") or "").strip().upper()
    client_id = (os.getenv("CASHFREE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("CASHFREE_CLIENT_SECRET") or "").strip()
    notify_url = (os.getenv("CASHFREE_WALLET_NOTIFY_URL") or "").strip()
    api_version = (os.getenv("CASHFREE_API_VERSION") or DEFAULT_API_VERSION).strip()
    if (
        environment not in _BASE_URLS
        or not client_id
        or not client_secret
        or not notify_url.startswith("https://")
    ):
        raise CashfreeNotConfigured()
    return CashfreeConfig(
        environment=environment,
        client_id=client_id,
        client_secret=client_secret,
        api_version=api_version,
        notify_url=notify_url,
    )


def paise_to_amount(amount_paise: int) -> float:
    return float(Decimal(amount_paise) / Decimal(100))


def amount_to_paise(value: Any) -> Optional[int]:
    """Exact 2-decimal rupee amount -> paise, else None."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    paise = dec * 100
    if paise != paise.to_integral_value():
        return None
    return int(paise)


def _headers(config: CashfreeConfig) -> Dict[str, str]:
    return {
        "x-api-version": config.api_version,
        "x-client-id": config.client_id,
        "x-client-secret": config.client_secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(config: CashfreeConfig, method: str, path: str, *, op: str, json_body=None) -> Dict[str, Any]:
    try:
        resp = requests.request(
            method,
            f"{config.base_url}{path}",
            headers=_headers(config),
            json=json_body,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
    except requests.Timeout:
        logger.warning("CASHFREE_CALL_FAILED op=%s kind=timeout", op)
        raise CashfreeError("timeout") from None
    except requests.RequestException:
        logger.warning("CASHFREE_CALL_FAILED op=%s kind=network", op)
        raise CashfreeError("network") from None

    if resp.status_code >= 400:
        logger.warning("CASHFREE_CALL_FAILED op=%s kind=http status=%s", op, resp.status_code)
        raise CashfreeError(
            "http",
            status_code=resp.status_code,
            definite=400 <= resp.status_code < 500,
        )
    try:
        body = resp.json()
    except ValueError:
        logger.warning("CASHFREE_CALL_FAILED op=%s kind=parse", op)
        raise CashfreeError("parse") from None
    if not isinstance(body, dict):
        raise CashfreeError("parse")
    return body


def create_order(
    config: CashfreeConfig,
    *,
    order_id: str,
    amount_paise: int,
    customer_id: str,
    customer_phone: str,
    customer_name: Optional[str],
    customer_email: Optional[str],
    expires_at_ist: datetime,
) -> Dict[str, Any]:
    customer: Dict[str, Any] = {
        "customer_id": customer_id,
        "customer_phone": customer_phone,
    }
    if customer_name:
        customer["customer_name"] = customer_name[:100]
    if customer_email:
        customer["customer_email"] = customer_email
    body = {
        "order_id": order_id,
        "order_amount": paise_to_amount(amount_paise),
        "order_currency": "INR",
        "customer_details": customer,
        "order_meta": {"notify_url": config.notify_url},
        "order_expiry_time": expires_at_ist.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30",
        "order_note": "OpenBid wallet recharge",
    }
    return _request(config, "POST", "/pg/orders", op="create_order", json_body=body)


def get_order(config: CashfreeConfig, order_id: str) -> Dict[str, Any]:
    return _request(config, "GET", f"/pg/orders/{order_id}", op="get_order")


def verify_webhook_signature(
    *,
    raw_body: bytes,
    timestamp: Optional[str],
    signature: Optional[str],
    secret: str,
) -> bool:
    """Cashfree PG webhook: base64(HMAC-SHA256(secret, timestamp + raw_body))."""
    if not timestamp or not signature or not secret:
        return False
    if not timestamp.isdigit() or not (10 <= len(timestamp) <= 16):
        return False
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + raw_body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())
