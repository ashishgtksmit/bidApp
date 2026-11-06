"""Internal authorization for high-risk generic routes (PR25 / PR31).

Ordinary mobile JWTs are insufficient. Callers must also present
``X-OpenBid-Internal-Key`` matching ``INTERNAL_NOTIFICATION_KEY``.

Fails closed when the environment secret is missing or empty.
Never logs provided or expected key values.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


INTERNAL_NOTIFICATION_HEADER = "X-OpenBid-Internal-Key"
INTERNAL_NOTIFICATION_ACCESS_REQUIRED = "INTERNAL_NOTIFICATION_ACCESS_REQUIRED"
INTERNAL_EMAIL_ACCESS_REQUIRED = "INTERNAL_EMAIL_ACCESS_REQUIRED"


def _keys_match(provided: str, expected: str) -> bool:
    """Constant-time compare; unequal lengths never match."""
    provided_b = provided.encode("utf-8")
    expected_b = expected.encode("utf-8")
    if len(provided_b) != len(expected_b):
        # Keep a compare call for roughly similar work, then fail.
        secrets.compare_digest(expected_b, expected_b)
        return False
    return secrets.compare_digest(provided_b, expected_b)


def _require_internal_key(
    internal_key: str | None,
    *,
    detail: str,
) -> None:
    expected = (os.getenv("INTERNAL_NOTIFICATION_KEY") or "").strip()
    provided = internal_key or ""
    if not expected or not _keys_match(provided, expected):
        raise HTTPException(
            status_code=403,
            detail=detail,
        )


async def require_internal_notification_access(
    internal_key: str | None = Header(
        default=None,
        alias=INTERNAL_NOTIFICATION_HEADER,
    ),
) -> None:
    """PR25 — generic notification dispatch routes."""
    _require_internal_key(
        internal_key,
        detail=INTERNAL_NOTIFICATION_ACCESS_REQUIRED,
    )


WALLET_OPS_HEADER = "X-OpenBid-Wallet-Ops-Key"
WALLET_OPS_ACCESS_REQUIRED = "WALLET_OPS_ACCESS_REQUIRED"


async def require_wallet_ops_access(
    ops_key: str | None = Header(default=None, alias=WALLET_OPS_HEADER),
) -> None:
    """PR48 — wallet withdrawal ops. Separate secret ``WALLET_OPS_KEY`` so the
    notification/email key cannot move money. Fails closed when unset."""
    expected = (os.getenv("WALLET_OPS_KEY") or "").strip()
    if not expected or not _keys_match(ops_key or "", expected):
        raise HTTPException(status_code=403, detail=WALLET_OPS_ACCESS_REQUIRED)


async def require_internal_email_access(
    internal_key: str | None = Header(
        default=None,
        alias=INTERNAL_NOTIFICATION_HEADER,
    ),
) -> None:
    """PR31 — generic POST /sendemail (same shared secret, email-specific detail)."""
    _require_internal_key(
        internal_key,
        detail=INTERNAL_EMAIL_ACCESS_REQUIRED,
    )
