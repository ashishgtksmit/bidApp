"""Vendors, KYC, customers, account locks, cars and drivers."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.bid_details import BidDetail
from ..models.car_details import CarDetail
from ..models.driver_details import DriverDetail
from ..models.request_table import Request as TripRequest
from ..models.user_devices import UserDevice
from ..models.user_table import User
from ..models.vendor_subscription import VendorSubscriptionState
from ..models.vendor_wallet import VendorWallet
from ..utils.vendor_snapshot_refresh import request_snapshot_refresh
from .common import (
    ST_CONFIRMED,
    ST_HANDSHAKE,
    PageParams,
    csv_ids,
    iso,
    is_tombstone,
    mask_tail,
    to_float,
    today_ist,
)
from .security import CurrentAdmin, audit, require, require_reader

logger = logging.getLogger("openbid.admin")
router = APIRouter()


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class OptionalReasonIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)


class DocumentLinkIn(BaseModel):
    path: str = Field(min_length=3, max_length=500)


# --------------------------------------------------------------------------- helpers


def _vendor_state(u: User) -> str:
    if u.lockApp:
        return "locked"
    if u.alsoVendor and u.vendorApproved:
        return "approved"
    if u.alsoVendor:
        return "pending"
    return "customer"


def _user_row(u: User, *, pii: bool) -> dict:
    return {
        "uid": int(u.UID),
        "phone": u.userAppId if pii else mask_tail(u.userAppId),
        "fullName": u.fullName,
        "email": u.emailId if pii else None,
        "city": u.city,
        "state": u.state,
        "baseLocation": u.baseLocation,
        "gender": u.gender,
        "profilePicture": u.profilePicture,
        "isVendor": bool(u.alsoVendor),
        "vendorApproved": bool(u.vendorApproved),
        "locked": bool(u.lockApp),
        "vendorState": _vendor_state(u),
        "vendorRating": to_float(u.rating),
        "vendorReviews": int(u.totalNoOfReviews or 0),
        "customerRating": to_float(u.customerRating),
        "customerReviews": int(u.totalCustomerReviews or 0),
        "joinedOn": iso(u.joiningDate) or iso(u.custSignUpDate),
        "updatedAt": iso(u.tableTimestamp),
    }


def _get_user(db: Session, uid: int) -> User:
    u = db.query(User).filter(User.UID == uid).first()
    if u is None or is_tombstone(u.userAppId):
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
    return u


def _live_users(db: Session):
    return db.query(User).filter(~User.userAppId.like("%.DELETED%"))


def _search(query, q: Optional[str]):
    if not q:
        return query
    like = f"%{q.strip()}%"
    return query.filter(or_(User.userAppId.like(like), User.fullName.like(like), User.emailId.like(like),
                            User.city.like(like)))


def _refresh(phone: str, flag: str) -> None:
    try:
        request_snapshot_refresh(phone, flag=flag)
    except Exception:  # never fail the admin action on a snapshot refresh
        logger.warning("admin snapshot refresh failed flag=%s", flag)


# --------------------------------------------------------------------------- vendors


@router.get("/vendors")
def list_vendors(
    status: str = Query("all", pattern="^(all|pending|approved|locked)$"),
    q: Optional[str] = Query(None, max_length=100),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = _search(_live_users(db).filter(User.alsoVendor.is_(True)), q)
    if status == "pending":
        query = query.filter(User.vendorApproved.is_(False), User.lockApp.is_(False))
    elif status == "approved":
        query = query.filter(User.vendorApproved.is_(True), User.lockApp.is_(False))
    elif status == "locked":
        query = query.filter(User.lockApp.is_(True))
    rows, total = pg.apply(query.order_by(User.tableTimestamp.desc()))
    pii = admin.can("pii.read")
    phones = [r.userAppId for r in rows]
    fleet = dict(
        db.query(CarDetail.userAppId, func.count(CarDetail.CARID))
        .filter(CarDetail.userAppId.in_(phones or [""]), CarDetail.isDeleted.is_(False))
        .group_by(CarDetail.userAppId).all()
    )
    subs = {
        s.user_uid: s.current_period_end
        for s in db.query(VendorSubscriptionState).filter(
            VendorSubscriptionState.user_uid.in_([int(r.UID) for r in rows] or [0])
        )
    }
    now = datetime.utcnow()
    items = []
    for r in rows:
        row = _user_row(r, pii=pii)
        row["cars"] = int(fleet.get(r.userAppId, 0))
        end = subs.get(int(r.UID))
        row["subscription"] = "none" if end is None else ("active" if end > now else "expired")
        row["tripsCompleted"] = int(r.noOfTripsCompleted or 0)
        row["kycComplete"] = bool(r.imageAadhar and r.imagePAN)
        items.append(row)
    return pg.wrap(items, total)


@router.get("/users/{uid}")
def user_detail(
    uid: int,
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    u = _get_user(db, uid)
    pii = admin.can("pii.read")
    out = _user_row(u, pii=pii)
    out["address"] = u.address if pii else None
    out["dob"] = u.dob if pii else None
    out["alternateNumber"] = u.alternateNumber if pii else mask_tail(u.alternateNumber)
    out["tags"] = csv_ids(u.tags)
    out["preferences"] = {
        "regions": csv_ids(u.regionPreferences),
        "cities": csv_ids(u.cityPreferences),
        "requestTypes": csv_ids(u.requestTypePreferences),
    }
    out["kyc"] = {
        "aadhaar": u.imageAadhar if pii else None,
        "pan": u.imagePAN if pii else None,
        "passbook": u.imageBankAccount if pii else None,
    }
    out["bank"] = {
        "holderName": u.bankAccountHolderName,
        "accountNumber": mask_tail(u.bankAccountNo),
        "ifsc": u.bankIFSC,
        "bankName": u.bankName,
    } if u.bankAccountNo else None

    phone = u.userAppId
    today = today_ist()
    as_customer = db.query(TripRequest).filter(TripRequest.customerAppId == phone)
    as_vendor = db.query(TripRequest).filter(TripRequest.requestWonBy == phone)
    out["stats"] = {
        "requestsPosted": as_customer.count(),
        "tripsTaken": as_customer.filter(TripRequest.requestStatus == ST_CONFIRMED,
                                         TripRequest.pickUpDate < today).count(),
        "tripsServed": as_vendor.filter(TripRequest.requestStatus == ST_CONFIRMED,
                                        TripRequest.pickUpDate < today).count(),
        "upcomingAsVendor": as_vendor.filter(TripRequest.requestStatus.in_([ST_CONFIRMED, ST_HANDSHAKE]),
                                             TripRequest.pickUpDate >= today).count(),
        "bidsPlaced": db.query(BidDetail).filter(BidDetail.bidderID == phone).count(),
        "earningsGross": int(
            db.query(func.coalesce(func.sum(TripRequest.finalAmount), 0))
            .filter(TripRequest.requestWonBy == phone, TripRequest.requestStatus == ST_CONFIRMED,
                    TripRequest.pickUpDate < today).scalar() or 0
        ),
    }
    out["cars"] = [_car_row(c) for c in db.query(CarDetail).filter(CarDetail.userAppId == phone)
                   .order_by(CarDetail.CARID.desc()).all()]
    out["drivers"] = [_driver_row(d, pii=pii) for d in db.query(DriverDetail)
                      .filter(DriverDetail.userAppId == phone).all()]
    w = db.query(VendorWallet).filter(VendorWallet.user_uid == int(u.UID)).first()
    out["wallet"] = None if w is None else {
        "balancePaise": int(w.balance_paise), "heldPaise": int(w.held_paise),
        "availablePaise": max(int(w.balance_paise) - int(w.held_paise), 0),
    }
    s = db.query(VendorSubscriptionState).filter(VendorSubscriptionState.user_uid == int(u.UID)).first()
    out["subscription"] = None if s is None else {
        "currentPeriodEnd": iso(s.current_period_end),
        "autoRenew": bool(s.auto_renew),
        "active": bool(s.current_period_end and s.current_period_end > datetime.utcnow()),
    }
    devices = db.query(UserDevice).filter(UserDevice.userAppId == phone).all()
    shared = {}
    if devices:
        ids = [d.deviceId for d in devices]
        for d in db.query(UserDevice).filter(UserDevice.deviceId.in_(ids), UserDevice.userAppId != phone):
            shared.setdefault(d.deviceId, []).append(d.userAppId if pii else mask_tail(d.userAppId))
    out["devices"] = [
        {"deviceId": mask_tail(d.deviceId, 6), "lastSeenAt": iso(d.lastSeenAt),
         "sharedWith": shared.get(d.deviceId, [])}
        for d in devices
    ]
    return out


@router.post("/vendors/{uid}/approve")
def approve_vendor(
    uid: int,
    request: Request,
    background: BackgroundTasks,
    admin: CurrentAdmin = Depends(require("partners.write")),
    db: Session = Depends(get_db),
) -> dict:
    u = _get_user(db, uid)
    if not u.alsoVendor:
        raise HTTPException(status_code=409, detail="NOT_A_VENDOR_APPLICANT")
    if u.vendorApproved:
        return _user_row(u, pii=admin.can("pii.read"))
    before = {"vendorApproved": False}
    u.vendorApproved = True
    tags = [t for t in (u.tags or "").split(",") if t.strip()]
    if "1" not in [t.strip() for t in tags]:  # same as legacy update_vendor_approved_status
        tags.append("1")
        u.tags = ",".join(t.strip() for t in tags)
    u.tableTimestamp = datetime.now()
    audit(db, admin, request, action="vendor.approve", entity_type="user", entity_id=uid,
          summary=f"Approved vendor {u.fullName}", before=before, after={"vendorApproved": True})
    db.commit()
    background.add_task(_refresh, u.userAppId, "Vendor")
    return _user_row(u, pii=admin.can("pii.read"))


@router.post("/vendors/{uid}/revoke")
def revoke_vendor(
    uid: int,
    body: ReasonIn,
    request: Request,
    background: BackgroundTasks,
    admin: CurrentAdmin = Depends(require("partners.write")),
    db: Session = Depends(get_db),
) -> dict:
    u = _get_user(db, uid)
    if not u.vendorApproved:
        raise HTTPException(status_code=409, detail="VENDOR_NOT_APPROVED")
    u.vendorApproved = False
    u.tableTimestamp = datetime.now()
    audit(db, admin, request, action="vendor.revoke", entity_type="user", entity_id=uid,
          summary=body.reason, before={"vendorApproved": True}, after={"vendorApproved": False})
    db.commit()
    background.add_task(_refresh, u.userAppId, "Vendor")
    return _user_row(u, pii=admin.can("pii.read"))


@router.post("/vendors/{uid}/reject")
def reject_vendor_application(
    uid: int,
    body: ReasonIn,
    request: Request,
    admin: CurrentAdmin = Depends(require("partners.write")),
    db: Session = Depends(get_db),
) -> dict:
    """Reject a pending application. Unlike legacy ``/rejectuser`` the account is kept."""
    u = _get_user(db, uid)
    if not u.alsoVendor or u.vendorApproved:
        raise HTTPException(status_code=409, detail="NOT_A_PENDING_APPLICATION")
    u.alsoVendor = False
    u.tableTimestamp = datetime.now()
    audit(db, admin, request, action="vendor.reject", entity_type="user", entity_id=uid,
          summary=body.reason, before={"alsoVendor": True}, after={"alsoVendor": False})
    db.commit()
    return _user_row(u, pii=admin.can("pii.read"))


@router.post("/users/{uid}/lock")
def lock_account(
    uid: int,
    body: ReasonIn,
    request: Request,
    background: BackgroundTasks,
    admin: CurrentAdmin = Depends(require("accounts.lock")),
    db: Session = Depends(get_db),
) -> dict:
    u = _get_user(db, uid)
    if u.lockApp:
        raise HTTPException(status_code=409, detail="ALREADY_LOCKED")
    u.lockApp = True
    u.tableTimestamp = datetime.now()
    audit(db, admin, request, action="account.lock", entity_type="user", entity_id=uid,
          summary=body.reason, before={"lockApp": False}, after={"lockApp": True})
    db.commit()
    background.add_task(_refresh, u.userAppId, "Vendor" if u.alsoVendor else "Customer")
    return _user_row(u, pii=admin.can("pii.read"))


@router.post("/users/{uid}/unlock")
def unlock_account(
    uid: int,
    body: OptionalReasonIn,
    request: Request,
    background: BackgroundTasks,
    admin: CurrentAdmin = Depends(require("accounts.lock")),
    db: Session = Depends(get_db),
) -> dict:
    u = _get_user(db, uid)
    if not u.lockApp:
        raise HTTPException(status_code=409, detail="NOT_LOCKED")
    u.lockApp = False
    u.tableTimestamp = datetime.now()
    audit(db, admin, request, action="account.unlock", entity_type="user", entity_id=uid,
          summary=body.reason, before={"lockApp": True}, after={"lockApp": False})
    db.commit()
    background.add_task(_refresh, u.userAppId, "Vendor" if u.alsoVendor else "Customer")
    return _user_row(u, pii=admin.can("pii.read"))


@router.post("/documents/link")
def document_link(
    body: DocumentLinkIn,
    request: Request,
    admin: CurrentAdmin = Depends(require("pii.read")),
    db: Session = Depends(get_db),
) -> dict:
    """Short-lived signed URL for a private KYC / fleet document. Every view is audited."""
    path = body.path.strip()
    if path.startswith("http") and "?" not in path:
        url = path  # already a public blob URL (e.g. profile picture)
    else:
        try:
            from ..utils.image import generate_vendor_document_sas
            url = generate_vendor_document_sas(blob_path=path)
        except Exception:
            raise HTTPException(status_code=502, detail="DOCUMENT_LINK_FAILED")
    audit(db, admin, request, action="document.view", entity_type="document", entity_id=path[-64:],
          commit=True)
    return {"url": url}


# --------------------------------------------------------------------------- customers


@router.get("/customers")
def list_customers(
    status: str = Query("all", pattern="^(all|active|locked)$"),
    q: Optional[str] = Query(None, max_length=100),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = _search(_live_users(db), q)
    if status == "locked":
        query = query.filter(User.lockApp.is_(True))
    elif status == "active":
        query = query.filter(User.lockApp.is_(False))
    rows, total = pg.apply(query.order_by(User.UID.desc()))
    phones = [r.userAppId for r in rows] or [""]
    posted = dict(db.query(TripRequest.customerAppId, func.count(TripRequest.RID))
                  .filter(TripRequest.customerAppId.in_(phones)).group_by(TripRequest.customerAppId).all())
    pii = admin.can("pii.read")
    items = []
    for r in rows:
        row = _user_row(r, pii=pii)
        row["requestsPosted"] = int(posted.get(r.userAppId, 0))
        row["tripsTaken"] = int(r.custNoOfTripsCompleted or 0)
        items.append(row)
    return pg.wrap(items, total)


# --------------------------------------------------------------------------- cars


def _car_row(c: CarDetail) -> dict:
    if c.isDeleted:
        state = "removed"
    elif c.adminApproved:
        state = "approved"
    else:
        state = "pending"
    return {
        "carId": int(c.CARID),
        "ownerPhone": c.userAppId,
        "regNo": c.carRegNo,
        "model": c.carModel,
        "modelYear": c.modelYear,
        "color": c.carColor,
        "ownerName": c.ownerName,
        "ownedByVendor": bool(c.carOwnedBySameVendor),
        "carTypeId": int(c.CTD) if c.CTD is not None else None,
        "state": state,
        "registeredOn": iso(c.registeredOn),
        "docs": {
            "rc": c.registrationDoc,
            "poa": c.powerOfAttorneyDoc,
            "front": c.imageVehicleFront,
            "side": c.imageVehicleSide,
        },
    }


@router.get("/cars")
def list_cars(
    status: str = Query("all", pattern="^(all|pending|approved|removed)$"),
    q: Optional[str] = Query(None, max_length=100),
    pg: PageParams = Depends(),
    _: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(CarDetail)
    if status == "pending":
        query = query.filter(CarDetail.adminApproved.is_(False), CarDetail.isDeleted.is_(False))
    elif status == "approved":
        query = query.filter(CarDetail.adminApproved.is_(True), CarDetail.isDeleted.is_(False))
    elif status == "removed":
        query = query.filter(CarDetail.isDeleted.is_(True))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(CarDetail.carRegNo.like(like), CarDetail.normalizedCarRegNo.like(like),
                                 CarDetail.carModel.like(like), CarDetail.userAppId.like(like),
                                 CarDetail.ownerName.like(like)))
    rows, total = pg.apply(query.order_by(CarDetail.CARID.desc()))
    owners = {u.userAppId: u.fullName for u in db.query(User.userAppId, User.fullName)
              .filter(User.userAppId.in_([r.userAppId for r in rows] or [""]))}
    items = []
    for r in rows:
        row = _car_row(r)
        row["vendorName"] = owners.get(r.userAppId)
        items.append(row)
    return pg.wrap(items, total)


def _car_in_active_use(db: Session, car_id: int) -> bool:
    today = today_ist()
    return (
        db.query(BidDetail.BID)
        .join(TripRequest, TripRequest.RID == BidDetail.rID)
        .filter(
            BidDetail.CARID == car_id,
            TripRequest.requestStatus.in_([ST_HANDSHAKE, ST_CONFIRMED]),
            func.coalesce(TripRequest.tripEndDate, TripRequest.pickUpDate) >= today,
            TripRequest.requestWonBy == BidDetail.bidderID,
        )
        .first()
        is not None
    )


@router.post("/cars/{car_id}/approve")
def approve_car(
    car_id: int,
    request: Request,
    admin: CurrentAdmin = Depends(require("fleet.write")),
    db: Session = Depends(get_db),
) -> dict:
    c = db.query(CarDetail).filter(CarDetail.CARID == car_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="CAR_NOT_FOUND")
    if c.isDeleted:
        raise HTTPException(status_code=409, detail="CAR_REMOVED")
    if not c.adminApproved:
        c.adminApproved = True
        audit(db, admin, request, action="car.approve", entity_type="car", entity_id=car_id,
              summary=f"Approved {c.carRegNo}", before={"adminApproved": False}, after={"adminApproved": True})
        db.commit()
    return _car_row(c)


@router.post("/cars/{car_id}/reject")
def reject_car(
    car_id: int,
    body: ReasonIn,
    request: Request,
    admin: CurrentAdmin = Depends(require("fleet.write")),
    db: Session = Depends(get_db),
) -> dict:
    """Unapprove and remove a car (soft delete, same columns as PR15)."""
    c = db.query(CarDetail).filter(CarDetail.CARID == car_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="CAR_NOT_FOUND")
    if c.isDeleted:
        raise HTTPException(status_code=409, detail="CAR_REMOVED")
    if _car_in_active_use(db, car_id):
        raise HTTPException(status_code=409, detail="CAR_IN_ACTIVE_USE")
    before = _car_row(c)
    c.adminApproved = False
    c.isDeleted = True
    c.deletedAt = datetime.utcnow()
    c.deletedBy = "ADMIN"
    audit(db, admin, request, action="car.reject", entity_type="car", entity_id=car_id,
          summary=body.reason, before={"state": before["state"]}, after={"state": "removed"})
    db.commit()
    return _car_row(c)


# --------------------------------------------------------------------------- drivers


def _driver_row(d: DriverDetail, *, pii: bool) -> dict:
    return {
        "driverId": int(d.DDID),
        "vendorPhone": d.userAppId if pii else mask_tail(d.userAppId),
        "name": d.driverName,
        "phone": d.driverNumber if pii else mask_tail(d.driverNumber),
        "dob": iso(d.driverDOB) if pii else None,
        "gender": d.driverGender,
        "city": d.driverCity,
        "photo": d.driverPhoto,
        "docs": {"licence": d.driverLicense, "id": d.driverDocument} if pii else None,
        "updatedAt": iso(d.tableTimestamp),
    }


@router.get("/drivers")
def list_drivers(
    q: Optional[str] = Query(None, max_length=100),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(DriverDetail)
    if hasattr(DriverDetail, "isDeleted"):
        query = query.filter(DriverDetail.isDeleted.is_(False))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(DriverDetail.driverName.like(like), DriverDetail.driverNumber.like(like),
                                 DriverDetail.driverCity.like(like), DriverDetail.userAppId.like(like)))
    rows, total = pg.apply(query.order_by(DriverDetail.DDID.desc()))
    vendors = {u.userAppId: u.fullName for u in db.query(User.userAppId, User.fullName)
               .filter(User.userAppId.in_([r.userAppId for r in rows] or [""]))}
    today = today_ist()
    busy = {
        r[0] for r in db.query(TripRequest.driverAssignedID).filter(
            TripRequest.requestStatus == ST_CONFIRMED,
            func.coalesce(TripRequest.tripEndDate, TripRequest.pickUpDate) >= today,
            TripRequest.driverAssignedID.in_([int(r.DDID) for r in rows] or [0]),
        )
    }
    pii = admin.can("pii.read")
    items = []
    for r in rows:
        row = _driver_row(r, pii=pii)
        row["vendorName"] = vendors.get(r.userAppId)
        row["onUpcomingTrip"] = int(r.DDID) in busy
        items.append(row)
    return pg.wrap(items, total)
