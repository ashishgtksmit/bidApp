"""Wallets, recharges, withdrawals (payout queue) and subscriptions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..crud import vendor_subscription as subscription_crud
from ..crud import vendor_wallet_withdrawal as withdrawal_crud
from ..database import get_db
from ..models.user_table import User
from ..models.vendor_subscription import (
    VendorSubscriptionPeriod,
    VendorSubscriptionPlan,
    VendorSubscriptionState,
)
from ..models.vendor_wallet import (
    VendorWallet,
    VendorWalletRecharge,
    VendorWalletTransaction,
    VendorWalletWithdrawal,
)
from ..schemas.vendor_subscription import OpsSubscriptionGrantRequest, OpsSubscriptionPlanUpdate
from .common import PageParams, iso, mask_tail
from .security import CurrentAdmin, audit, require, require_reader

router = APIRouter()

_WITHDRAWAL_REF = r"^[A-Za-z0-9_-]{6,40}$"


def _users_by_uid(db: Session, uids: list[int]) -> dict[int, User]:
    if not uids:
        return {}
    return {int(u.UID): u for u in db.query(User).filter(User.UID.in_(uids))}


def _who(u: Optional[User], pii: bool) -> dict:
    if u is None:
        return {"name": None, "phone": None}
    return {"name": u.fullName, "phone": u.userAppId if pii else mask_tail(u.userAppId)}


# --------------------------------------------------------------------------- wallets


@router.get("/wallets")
def list_wallets(
    q: Optional[str] = Query(None, max_length=100),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(VendorWallet, User).join(User, User.UID == VendorWallet.user_uid)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(User.fullName.like(like), User.userAppId.like(like)))
    rows, total = pg.apply(query.order_by(VendorWallet.balance_paise.desc()))
    pii = admin.can("pii.read")
    totals = db.query(func.coalesce(func.sum(VendorWallet.balance_paise), 0),
                      func.coalesce(func.sum(VendorWallet.held_paise), 0)).one()
    out = pg.wrap([
        {"uid": int(u.UID), **_who(u, pii), "isVendor": bool(u.alsoVendor),
         # balance_paise includes held funds (same as crud.vendor_wallet.get_wallet_summary).
         "balancePaise": int(w.balance_paise), "heldPaise": int(w.held_paise),
         "availablePaise": max(int(w.balance_paise) - int(w.held_paise), 0), "updatedAt": iso(w.updated_at)}
        for w, u in rows
    ], total)
    out["totals"] = {"balancePaise": int(totals[0]), "heldPaise": int(totals[1])}
    return out


@router.get("/wallets/{uid}/transactions")
def wallet_transactions(
    uid: int,
    pg: PageParams = Depends(),
    _: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    rows, total = pg.apply(db.query(VendorWalletTransaction).filter(VendorWalletTransaction.user_uid == uid)
                           .order_by(VendorWalletTransaction.id.desc()))
    return pg.wrap([
        {"ref": t.txn_ref, "direction": t.direction, "type": t.txn_type, "status": t.status,
         "amountPaise": int(t.amount_paise), "balanceAfterPaise": int(t.balance_after_paise),
         "description": t.description, "referenceId": t.reference_id, "side": t.source,
         "createdAt": iso(t.created_at)}
        for t in rows
    ], total)


# --------------------------------------------------------------------------- recharges


@router.get("/recharges")
def list_recharges(
    status: Optional[str] = Query(None, pattern="^(CREATE_PENDING|CREATE_FAILED|PENDING|PAID|FAILED|EXPIRED|REVIEW)$"),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(VendorWalletRecharge)
    if status == "REVIEW":
        query = query.filter(VendorWalletRecharge.ops_alerted_at.isnot(None), VendorWalletRecharge.status != "PAID")
    elif status:
        query = query.filter(VendorWalletRecharge.status == status)
    rows, total = pg.apply(query.order_by(VendorWalletRecharge.id.desc()))
    users = _users_by_uid(db, [int(r.user_uid) for r in rows])
    pii = admin.can("pii.read")
    return pg.wrap([
        {"orderId": r.order_id, **_who(users.get(int(r.user_uid)), pii), "uid": int(r.user_uid),
         "amountPaise": int(r.amount_paise), "status": r.status, "purpose": r.purpose or "WALLET",
         "side": r.source, "environment": r.environment, "failureCode": r.failure_code,
         "needsReview": r.ops_alerted_at is not None and r.status != "PAID",
         "createdAt": iso(r.created_at), "paidAt": iso(r.paid_at)}
        for r in rows
    ], total)


# --------------------------------------------------------------------------- withdrawals


class PaidIn(BaseModel):
    utr: str = Field(min_length=6, max_length=40, pattern=r"^[A-Za-z0-9]+$")


class RejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)


@router.get("/withdrawals")
def list_withdrawals(
    status: Optional[str] = Query(None, pattern="^(REQUESTED|PAID|REJECTED)$"),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(VendorWalletWithdrawal)
    if status:
        query = query.filter(VendorWalletWithdrawal.status == status)
    order = VendorWalletWithdrawal.requested_at.asc() if status == "REQUESTED" else VendorWalletWithdrawal.id.desc()
    rows, total = pg.apply(query.order_by(order))
    users = _users_by_uid(db, [int(r.user_uid) for r in rows])
    finance = admin.can("money.write")
    return pg.wrap([
        {
            "ref": r.withdrawal_ref, "uid": int(r.user_uid), **_who(users.get(int(r.user_uid)), admin.can("pii.read")),
            "amountPaise": int(r.amount_paise), "feePaise": int(r.fee_paise),
            # bidApp holds amount + fee; the bank transfer is the full amount, the fee is kept.
            "payoutPaise": int(r.amount_paise),
            "totalDebitPaise": int(r.amount_paise) + int(r.fee_paise),
            "status": r.status, "side": r.source,
            # Full account number only for roles that can pay it out.
            "bank": {
                "holderName": r.bank_account_holder_name,
                "accountNumber": r.bank_account_no if finance else mask_tail(r.bank_account_no),
                "ifsc": r.bank_ifsc, "bankName": r.bank_name,
            },
            "bankVerification": r.bank_verification_note, "utr": r.utr, "rejectionReason": r.rejection_reason,
            "requestedAt": iso(r.requested_at), "resolvedAt": iso(r.resolved_at),
        }
        for r in rows
    ], total)


def _withdrawal_status(db: Session, ref: str) -> Optional[str]:
    row = db.query(VendorWalletWithdrawal.status).filter(VendorWalletWithdrawal.withdrawal_ref == ref).first()
    return row[0] if row else None


@router.post("/withdrawals/{ref}/paid")
def withdrawal_paid(
    body: PaidIn,
    request: Request,
    background: BackgroundTasks,
    ref: str = Path(..., pattern=_WITHDRAWAL_REF),
    admin: CurrentAdmin = Depends(require("money.write")),
    db: Session = Depends(get_db),
) -> dict:
    # Reuses the PR48 ledger path (row lock, hold release, vendor push) unchanged.
    # A repeat with the same UTR is idempotent there; audit only the first change.
    was_pending = _withdrawal_status(db, ref) == "REQUESTED"
    item = withdrawal_crud.ops_mark_paid(db, ref, body.utr, background)
    if was_pending:
        audit(db, admin, request, action="withdrawal.paid", entity_type="withdrawal", entity_id=ref,
              summary=f"Paid with UTR {body.utr.upper()}", after={"status": "PAID", "utr": body.utr.upper()},
              commit=True)
    return item.model_dump() if hasattr(item, "model_dump") else dict(item)


@router.post("/withdrawals/{ref}/rejected")
def withdrawal_rejected(
    body: RejectIn,
    request: Request,
    background: BackgroundTasks,
    ref: str = Path(..., pattern=_WITHDRAWAL_REF),
    admin: CurrentAdmin = Depends(require("money.write")),
    db: Session = Depends(get_db),
) -> dict:
    was_pending = _withdrawal_status(db, ref) == "REQUESTED"
    item = withdrawal_crud.ops_mark_rejected(db, ref, body.reason, background)
    if was_pending:
        audit(db, admin, request, action="withdrawal.rejected", entity_type="withdrawal", entity_id=ref,
              summary=body.reason, after={"status": "REJECTED"}, commit=True)
    return item.model_dump() if hasattr(item, "model_dump") else dict(item)


# --------------------------------------------------------------------------- subscriptions


class GrantIn(BaseModel):
    userAppId: str = Field(min_length=10, max_length=15, pattern=r"^\d+$")
    days: int = Field(ge=1, le=366)
    reason: str = Field(min_length=3, max_length=200)


@router.get("/subscriptions/plans")
def list_plans(_: CurrentAdmin = Depends(require_reader), db: Session = Depends(get_db)) -> list[dict]:
    active = dict(
        db.query(VendorSubscriptionPeriod.plan_code, func.count(VendorSubscriptionPeriod.id))
        .group_by(VendorSubscriptionPeriod.plan_code).all()
    )
    return [
        {"code": p.code, "name": p.name, "pricePaise": int(p.price_paise), "durationDays": int(p.duration_days),
         "active": bool(p.active), "periodsSold": int(active.get(p.code, 0)), "updatedAt": iso(p.updated_at)}
        for p in db.query(VendorSubscriptionPlan).order_by(VendorSubscriptionPlan.price_paise).all()
    ]


@router.put("/subscriptions/plans/{code}")
def upsert_plan(
    body: OpsSubscriptionPlanUpdate,
    request: Request,
    code: str = Path(..., min_length=1, max_length=16, pattern=r"^[A-Z0-9_]+$"),
    admin: CurrentAdmin = Depends(require("subscriptions.write")),
    db: Session = Depends(get_db),
) -> dict:
    existing = db.query(VendorSubscriptionPlan).filter(VendorSubscriptionPlan.code == code).first()
    before = None if existing is None else {"name": existing.name, "pricePaise": int(existing.price_paise),
                                            "durationDays": int(existing.duration_days), "active": bool(existing.active)}
    item = subscription_crud.ops_upsert_plan(db, code, body)
    after = body.model_dump(exclude_none=True)
    audit(db, admin, request, action="plan.upsert", entity_type="plan", entity_id=code,
          summary=("Created" if before is None else "Updated") + f" plan {code}", before=before, after=after,
          commit=True)
    return item.model_dump() if hasattr(item, "model_dump") else dict(item)


@router.post("/subscriptions/grant")
def grant_subscription(
    body: GrantIn,
    request: Request,
    background: BackgroundTasks,
    admin: CurrentAdmin = Depends(require("subscriptions.write")),
    db: Session = Depends(get_db),
) -> dict:
    payload = OpsSubscriptionGrantRequest(
        userAppId=body.userAppId, days=body.days, reason=body.reason,
        idempotencyKey=f"admin-{admin.id}-{uuid.uuid4().hex[:24]}",
    )
    res = subscription_crud.ops_grant(db, payload, background)
    audit(db, admin, request, action="subscription.grant", entity_type="user", entity_id=body.userAppId,
          summary=f"{body.days} free days: {body.reason}", after={"days": body.days}, commit=True)
    return res.model_dump() if hasattr(res, "model_dump") else dict(res)


@router.get("/subscriptions/subscribers")
def list_subscribers(
    status: str = Query("all", pattern="^(all|active|expiring|expired)$"),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    now = datetime.utcnow()
    query = db.query(VendorSubscriptionState, User).join(User, User.UID == VendorSubscriptionState.user_uid)
    if status == "active":
        query = query.filter(VendorSubscriptionState.current_period_end > now)
    elif status == "expiring":
        query = query.filter(VendorSubscriptionState.current_period_end > now,
                             VendorSubscriptionState.current_period_end <= now + timedelta(days=7))
    elif status == "expired":
        query = query.filter(VendorSubscriptionState.current_period_end <= now)
    rows, total = pg.apply(query.order_by(VendorSubscriptionState.current_period_end.asc()))
    pii = admin.can("pii.read")
    return pg.wrap([
        {"uid": int(u.UID), **_who(u, pii), "currentPeriodEnd": iso(s.current_period_end),
         "autoRenew": bool(s.auto_renew),
         "state": "active" if s.current_period_end and s.current_period_end > now else "expired",
         "daysLeft": None if not s.current_period_end else (s.current_period_end - now).days}
        for s, u in rows
    ], total)
