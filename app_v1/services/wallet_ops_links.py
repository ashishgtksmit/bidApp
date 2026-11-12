"""Signed one-time ops links for wallet withdrawal email actions.

Tokens are HMAC-signed with ``WALLET_OPS_KEY`` (purpose ``wallet-ops-link-v1``).
One-time use is enforced by storing SHA-256(jti) on the withdrawal row and
clearing both action hashes after a successful submit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.vendor_wallet import VendorWalletWithdrawal

IST = ZoneInfo("Asia/Kolkata")
PURPOSE = "wallet-ops-link-v1"
ACTION_PAID = "paid"
ACTION_REJECTED = "rejected"
OpsAction = Literal["paid", "rejected"]

DEFAULT_TTL_SECONDS = 604_800  # 7 days


@dataclass(frozen=True)
class OpsLinkClaims:
    withdrawal_ref: str
    action: OpsAction
    jti: str
    exp: int


def ops_link_ttl_seconds() -> int:
    return int(os.getenv("WALLET_OPS_LINK_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))


def _ops_key() -> str:
    key = (os.getenv("WALLET_OPS_KEY") or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="WALLET_OPS_LINKS_UNAVAILABLE")
    return key


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _sign_body(body: str) -> str:
    material = f"{PURPOSE}:{body}".encode("utf-8")
    return hmac.new(_ops_key().encode("utf-8"), material, hashlib.sha256).hexdigest()


def hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _ist_now_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def mint_ops_link_token(
    *,
    withdrawal_ref: str,
    action: OpsAction,
    ttl_seconds: Optional[int] = None,
) -> Tuple[str, str, datetime]:
    """Return ``(token, jti, expires_at_naive_ist)``."""
    if action not in (ACTION_PAID, ACTION_REJECTED):
        raise ValueError("invalid ops link action")
    ttl = int(ttl_seconds if ttl_seconds is not None else ops_link_ttl_seconds())
    if ttl <= 0:
        raise ValueError("invalid ops link ttl")
    jti = secrets.token_hex(16)
    exp = int(time.time()) + ttl
    payload = {
        "wr": withdrawal_ref,
        "a": action,
        "jti": jti,
        "exp": exp,
    }
    body = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    token = f"{body}.{_sign_body(body)}"
    expires_at = _ist_now_naive() + timedelta(seconds=ttl)
    return token, jti, expires_at


def mint_ops_link_pair(
    row: VendorWalletWithdrawal,
) -> Tuple[str, str]:
    """Mint paid + reject tokens and store jti hashes on ``row`` (no commit)."""
    paid_token, paid_jti, expires_at = mint_ops_link_token(
        withdrawal_ref=row.withdrawal_ref, action=ACTION_PAID
    )
    reject_token, reject_jti, _ = mint_ops_link_token(
        withdrawal_ref=row.withdrawal_ref, action=ACTION_REJECTED
    )
    row.ops_link_paid_jti_hash = hash_jti(paid_jti)
    row.ops_link_reject_jti_hash = hash_jti(reject_jti)
    row.ops_link_expires_at = expires_at
    return paid_token, reject_token


def parse_ops_link_token(token: str) -> OpsLinkClaims:
    raw = (token or "").strip()
    if not raw or "." not in raw:
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    body, sig = raw.rsplit(".", 1)
    if not body or not sig:
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    expected = _sign_body(body)
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID") from None
    try:
        withdrawal_ref = str(payload["wr"])
        action = str(payload["a"])
        jti = str(payload["jti"])
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID") from None
    if action not in (ACTION_PAID, ACTION_REJECTED):
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    if not jti or len(jti) > 64:
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    if exp <= int(time.time()):
        raise HTTPException(status_code=410, detail="OPS_LINK_EXPIRED")
    return OpsLinkClaims(
        withdrawal_ref=withdrawal_ref,
        action=action,  # type: ignore[arg-type]
        jti=jti,
        exp=exp,
    )


def stored_jti_hash_for_action(row: VendorWalletWithdrawal, action: OpsAction) -> Optional[str]:
    if action == ACTION_PAID:
        return row.ops_link_paid_jti_hash
    return row.ops_link_reject_jti_hash


def verify_ops_link_against_row(
    row: VendorWalletWithdrawal,
    *,
    withdrawal_ref: str,
    action: OpsAction,
    token: str,
) -> OpsLinkClaims:
    """Validate signature, binding, expiry, and unused jti. Does not consume."""
    claims = parse_ops_link_token(token)
    if claims.withdrawal_ref != withdrawal_ref or claims.action != action:
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    if row.withdrawal_ref != withdrawal_ref:
        raise HTTPException(status_code=403, detail="OPS_LINK_INVALID")
    if row.ops_link_expires_at is not None and row.ops_link_expires_at <= _ist_now_naive():
        raise HTTPException(status_code=410, detail="OPS_LINK_EXPIRED")
    expected_hash = stored_jti_hash_for_action(row, action)
    if not expected_hash or not hmac.compare_digest(expected_hash, hash_jti(claims.jti)):
        raise HTTPException(status_code=410, detail="OPS_LINK_USED")
    return claims


def consume_ops_link_tokens(row: VendorWalletWithdrawal) -> None:
    """Clear both action hashes after a successful paid/reject via link."""
    row.ops_link_paid_jti_hash = None
    row.ops_link_reject_jti_hash = None
    row.ops_link_expires_at = None


def public_ops_link_url(
    *,
    api_base: str,
    withdrawal_ref: str,
    action: OpsAction,
    token: str,
) -> str:
    base = (api_base or "").rstrip("/")
    return (
        f"{base}/internal/wallet/withdrawals/{withdrawal_ref}/ops/{action}"
        f"?t={token}"
    )
