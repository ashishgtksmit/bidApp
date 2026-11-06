"""PR49 Cashfree Secure ID — Bank Account Verification Sync V2 (penny drop).

POST {base}/bank-account/sync with headers x-client-id / x-client-secret and
body {bank_account, ifsc, name, phone}. Response fields used: account_status
(VALID | INVALID), account_status_code, name_match_result, name_at_bank,
reference_id.

Configuration (environment / Azure App Settings only):
  BANK_VERIFICATION_ENABLED              "1" to enable (default off)
  CASHFREE_VERIFICATION_ENV              SANDBOX | PRODUCTION (default CASHFREE_ENV)
  CASHFREE_VERIFICATION_CLIENT_ID
  CASHFREE_VERIFICATION_CLIENT_SECRET

Never logs credentials, account numbers, or names.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from .cashfree_pg import CashfreeError, CashfreeNotConfigured

logger = logging.getLogger(__name__)

_BASE_URLS = {
    "SANDBOX": "https://sandbox.cashfree.com/verification",
    "PRODUCTION": "https://api.cashfree.com/verification",
}
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 30

# 400 validation codes that mean the vendor's details are wrong (not our config).
_INVALID_DETAIL_CODE_PREFIXES = ("bank_account_", "ifsc_")


@dataclass(frozen=True)
class VerificationConfig:
    environment: str
    client_id: str
    client_secret: str

    @property
    def base_url(self) -> str:
        return _BASE_URLS[self.environment]


def verification_enabled() -> bool:
    return (os.getenv("BANK_VERIFICATION_ENABLED") or "").strip() == "1"


def load_verification_config() -> VerificationConfig:
    environment = (
        os.getenv("CASHFREE_VERIFICATION_ENV") or os.getenv("CASHFREE_ENV") or ""
    ).strip().upper()
    client_id = (os.getenv("CASHFREE_VERIFICATION_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("CASHFREE_VERIFICATION_CLIENT_SECRET") or "").strip()
    if environment not in _BASE_URLS or not client_id or not client_secret:
        raise CashfreeNotConfigured()
    return VerificationConfig(environment, client_id, client_secret)


def clean_holder_name(name: str) -> str:
    """Cashfree allows only alphanumerics, space, . - / & (max 100)."""
    cleaned = re.sub(r"[^A-Za-z0-9 .\-/&]", "", str(name or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:100]


@dataclass(frozen=True)
class BankVerificationResult:
    account_valid: bool
    account_status_code: Optional[str]
    name_match_result: Optional[str]
    name_at_bank: Optional[str]
    reference_id: Optional[str]


def verify_bank_account(
    config: VerificationConfig,
    *,
    bank_account: str,
    ifsc: str,
    name: str,
    phone: Optional[str] = None,
) -> BankVerificationResult:
    """Returns a definite result, or raises ``CashfreeError``.

    Wrong vendor details (400 bank_account_*/ifsc_*, 422 fraud_account) are
    returned as an invalid result, not raised. Everything else (auth, IP
    whitelist, service not enabled, 5xx, timeouts) raises with
    ``definite=False`` so the caller can retry later without caching.
    """
    body: Dict[str, Any] = {
        "bank_account": str(bank_account).strip(),
        "ifsc": str(ifsc).strip().upper(),
        "name": clean_holder_name(name),
    }
    digits = re.sub(r"\D", "", str(phone or ""))
    if 8 <= len(digits) <= 13:
        body["phone"] = digits
    try:
        response = requests.post(
            f"{config.base_url}/bank-account/sync",
            json=body,
            headers={
                "x-client-id": config.client_id,
                "x-client-secret": config.client_secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
    except requests.Timeout:
        raise CashfreeError("timeout") from None
    except requests.RequestException:
        raise CashfreeError("network") from None

    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise CashfreeError("bad_json", status_code=response.status_code)

    status = response.status_code
    if status == 200:
        account_status = str(data.get("account_status") or "").upper()
        if account_status not in ("VALID", "INVALID"):
            raise CashfreeError("unknown_account_status", status_code=status)
        return BankVerificationResult(
            account_valid=account_status == "VALID",
            account_status_code=_opt(data.get("account_status_code")),
            name_match_result=_opt(data.get("name_match_result")),
            name_at_bank=_opt(data.get("name_at_bank")),
            reference_id=_opt(data.get("reference_id")),
        )

    code = str(data.get("code") or "")
    if status == 400 and code.startswith(_INVALID_DETAIL_CODE_PREFIXES):
        return BankVerificationResult(False, code.upper(), None, None, None)
    if status == 422 and code == "fraud_account":
        return BankVerificationResult(False, "FRAUD_ACCOUNT", None, None, None)
    logger.error("BANK_VERIFICATION_GATEWAY_ERROR status=%s code=%s", status, code[:60])
    raise CashfreeError("http", status_code=status)


def _opt(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:255] or None
