"""Catalog (locations, regions, cities, car types, request types, sightseeing spots),
reviews, integrity signals and push notifications."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models.car_type_details import CarTypeDetail
from ..models.city_list import City
from ..models.customer_reviews import CustomerReview
from ..models.location_details import LocationDetail
from ..models.region_details import Region
from ..models.request_type_details import RequestType
from ..models.sightseeing import SightseeingSpot
from ..models.user_devices import UserDevice
from ..models.user_table import User
from ..models.vendor_reviews import VendorReview
from .common import PageParams, iso, mask_tail, to_float
from .security import CurrentAdmin, audit, require, require_reader

logger = logging.getLogger("openbid.admin")
router = APIRouter()


# --------------------------------------------------------------------------- catalog reads


@router.get("/catalog")
def catalog(_: CurrentAdmin = Depends(require_reader), db: Session = Depends(get_db)) -> dict:
    regions = {int(r.RDID): r.regionName for r in db.query(Region).all()}
    spot_counts = dict(db.query(SightseeingSpot.locationId, func.count(SightseeingSpot.spotId))
                       .group_by(SightseeingSpot.locationId).all())
    return {
        "regions": [{"id": k, "name": v} for k, v in sorted(regions.items())],
        "locations": [
            {"id": int(l.LID), "name": l.location, "shortCode": l.location_shortCode,
             "regionId": int(l.regionId), "region": regions.get(int(l.regionId)),
             "spots": int(spot_counts.get(int(l.LID), 0))}
            for l in db.query(LocationDetail).order_by(LocationDetail.location).all()
        ],
        "cities": [{"id": int(c.CLID), "name": c.cities, "state": c.state}
                   for c in db.query(City).order_by(City.cities).all()],
        "carTypes": [{"id": int(c.CTD), "type": c.car_type, "subType": c.car_sub_type, "capacity": c.capacity,
                      "imageUrl": c.image_url} for c in db.query(CarTypeDetail).order_by(CarTypeDetail.CTD).all()],
        "requestTypes": [{"id": int(t.RTDID), "name": t.requestType}
                         for t in db.query(RequestType).order_by(RequestType.RTDID).all()],
    }


# --------------------------------------------------------------------------- catalog writes


class LocationIn(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    shortCode: Optional[str] = Field(default=None, max_length=10)
    regionId: int = Field(ge=1)


@router.post("/catalog/locations")
def create_location(body: LocationIn, request: Request, admin: CurrentAdmin = Depends(require("catalog.write")),
                    db: Session = Depends(get_db)) -> dict:
    if db.query(Region).filter(Region.RDID == body.regionId).first() is None:
        raise HTTPException(status_code=422, detail="REGION_NOT_FOUND")
    name = body.name.strip()
    dup = db.query(LocationDetail).filter(func.lower(LocationDetail.location) == name.lower(),
                                          LocationDetail.regionId == body.regionId).first()
    if dup:
        raise HTTPException(status_code=409, detail="LOCATION_EXISTS")
    loc = LocationDetail(location=name, location_shortCode=(body.shortCode or "").strip() or None,
                         regionId=body.regionId)
    db.add(loc)
    db.flush()
    audit(db, admin, request, action="catalog.location.create", entity_type="location", entity_id=loc.LID,
          summary=f"Added {name}", after=body.model_dump())
    db.commit()
    return {"id": int(loc.LID), "name": loc.location, "shortCode": loc.location_shortCode, "regionId": loc.regionId}


@router.patch("/catalog/locations/{lid}")
def update_location(lid: int, body: LocationIn, request: Request,
                    admin: CurrentAdmin = Depends(require("catalog.write")), db: Session = Depends(get_db)) -> dict:
    loc = db.query(LocationDetail).filter(LocationDetail.LID == lid).first()
    if loc is None:
        raise HTTPException(status_code=404, detail="LOCATION_NOT_FOUND")
    before = {"name": loc.location, "shortCode": loc.location_shortCode, "regionId": loc.regionId}
    loc.location = body.name.strip()
    loc.location_shortCode = (body.shortCode or "").strip() or None
    loc.regionId = body.regionId
    audit(db, admin, request, action="catalog.location.update", entity_type="location", entity_id=lid,
          before=before, after=body.model_dump())
    db.commit()
    return {"id": int(loc.LID), "name": loc.location, "shortCode": loc.location_shortCode, "regionId": loc.regionId}


class SpotIn(BaseModel):
    locationId: int = Field(ge=1)
    spotName: str = Field(min_length=2, max_length=200)
    permitRequired: bool = False
    typicalDurationMins: Optional[int] = Field(default=None, ge=5, le=24 * 60)
    isActive: bool = True
    sortOrder: int = Field(default=0, ge=0, le=10000)


class SpotPatch(BaseModel):
    spotName: Optional[str] = Field(default=None, min_length=2, max_length=200)
    permitRequired: Optional[bool] = None
    typicalDurationMins: Optional[int] = Field(default=None, ge=5, le=24 * 60)
    isActive: Optional[bool] = None
    sortOrder: Optional[int] = Field(default=None, ge=0, le=10000)


def _spot_out(s: SightseeingSpot) -> dict:
    return {"id": int(s.spotId), "locationId": int(s.locationId), "code": s.spotCode, "name": s.spotName,
            "permitRequired": bool(s.permitRequired), "typicalDurationMins": s.typicalDurationMins,
            "isActive": bool(s.isActive), "sortOrder": int(s.sortOrder or 0), "updatedAt": iso(s.updatedAt)}


@router.get("/catalog/spots")
def list_spots(locationId: Optional[int] = None, _: CurrentAdmin = Depends(require_reader),
               db: Session = Depends(get_db)) -> list[dict]:
    q = db.query(SightseeingSpot)
    if locationId:
        q = q.filter(SightseeingSpot.locationId == locationId)
    return [_spot_out(s) for s in q.order_by(SightseeingSpot.locationId, SightseeingSpot.sortOrder,
                                             SightseeingSpot.spotName).all()]


def _spot_code(db: Session, location_id: int, name: str) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:48] or "SPOT"
    code = f"L{location_id}_{base}"[:60]
    n = 2
    while db.query(SightseeingSpot.spotId).filter(SightseeingSpot.spotCode == code).first():
        code = f"L{location_id}_{base}"[:58] + f"_{n}"
        n += 1
    return code


@router.post("/catalog/spots")
def create_spot(body: SpotIn, request: Request, admin: CurrentAdmin = Depends(require("catalog.write")),
                db: Session = Depends(get_db)) -> dict:
    if db.query(LocationDetail).filter(LocationDetail.LID == body.locationId).first() is None:
        raise HTTPException(status_code=422, detail="LOCATION_NOT_FOUND")
    now = datetime.utcnow()
    s = SightseeingSpot(locationId=body.locationId, spotCode=_spot_code(db, body.locationId, body.spotName),
                        spotName=body.spotName.strip(), permitRequired=body.permitRequired,
                        typicalDurationMins=body.typicalDurationMins, isActive=body.isActive,
                        sortOrder=body.sortOrder, createdAt=now, updatedAt=now)
    db.add(s)
    db.flush()
    audit(db, admin, request, action="catalog.spot.create", entity_type="spot", entity_id=s.spotId,
          summary=f"Added {s.spotName}", after=body.model_dump())
    db.commit()
    return _spot_out(s)


@router.patch("/catalog/spots/{spot_id}")
def update_spot(spot_id: int, body: SpotPatch, request: Request,
                admin: CurrentAdmin = Depends(require("catalog.write")), db: Session = Depends(get_db)) -> dict:
    s = db.query(SightseeingSpot).filter(SightseeingSpot.spotId == spot_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="SPOT_NOT_FOUND")
    before = _spot_out(s)
    changes = body.model_dump(exclude_none=True)
    if "spotName" in changes:
        s.spotName = changes["spotName"].strip()
    for k in ("permitRequired", "typicalDurationMins", "isActive", "sortOrder"):
        if k in changes:
            setattr(s, k, changes[k])
    s.updatedAt = datetime.utcnow()
    audit(db, admin, request, action="catalog.spot.update", entity_type="spot", entity_id=spot_id,
          summary=f"Updated {s.spotName}", before=before, after=changes)
    db.commit()
    return _spot_out(s)


# --------------------------------------------------------------------------- reviews


@router.get("/reviews")
def list_reviews(
    kind: Literal["vendor", "customer"] = "vendor",
    maxRating: Optional[float] = Query(None, ge=0.5, le=5),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    pii = admin.can("pii.read")
    if kind == "vendor":
        avg = (VendorReview.driverBehaviour + VendorReview.punctuality + VendorReview.carCondition
               + VendorReview.cleanliness) / 4
        q = db.query(VendorReview)
        if maxRating is not None:
            q = q.filter(avg <= maxRating)
        rows, total = pg.apply(q.order_by(VendorReview.VRID.desc()))
        uids = [int(r.VENDORID) for r in rows]
        vendors = {int(u.UID): u for u in db.query(User).filter(User.UID.in_(uids or [0]))}
        custs = {u.userAppId: u.fullName for u in db.query(User.userAppId, User.fullName)
                 .filter(User.userAppId.in_([r.customerAppId for r in rows] or [""]))}
        items = []
        for r in rows:
            scores = [to_float(r.driverBehaviour), to_float(r.punctuality), to_float(r.carCondition),
                      to_float(r.cleanliness)]
            v = vendors.get(int(r.VENDORID))
            items.append({
                "id": int(r.VRID), "rid": int(r.RID), "rating": round(sum(s or 0 for s in scores) / 4, 2),
                "scores": {"driverBehaviour": scores[0], "punctuality": scores[1], "carCondition": scores[2],
                           "cleanliness": scores[3]},
                "refreshments": bool(r.refreshments), "comments": r.comments,
                "aboutName": v.fullName if v else None, "byName": custs.get(r.customerAppId),
                "byPhone": r.customerAppId if pii else mask_tail(r.customerAppId),
                "createdAt": iso(r.tableTimestamp),
            })
        return pg.wrap(items, total)
    q = db.query(CustomerReview)
    if maxRating is not None:
        q = q.filter(CustomerReview.generalRating <= maxRating)
    rows, total = pg.apply(q.order_by(CustomerReview.CR.desc()))
    names = {u.userAppId: u.fullName for u in db.query(User.userAppId, User.fullName).filter(
        User.userAppId.in_([r.ratingGiverUserAppId for r in rows] + [r.ratingReceiverUserAppId for r in rows] or [""]))}
    return pg.wrap([
        {"id": int(r.CR), "rid": int(r.RID) if str(r.RID).isdigit() else r.RID,
         "rating": to_float(r.generalRating), "comments": r.comments,
         "aboutName": names.get(r.ratingReceiverUserAppId), "byName": names.get(r.ratingGiverUserAppId),
         "createdAt": iso(r.tableTimestamp)}
        for r in rows
    ], total)


# --------------------------------------------------------------------------- integrity (PR64)


@router.get("/integrity/shared-devices")
def shared_devices(
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    """Install devices used by more than one account — the PR64 related-party signal."""
    groups = (
        db.query(UserDevice.deviceId, func.count(func.distinct(UserDevice.userAppId)))
        .group_by(UserDevice.deviceId)
        .having(func.count(func.distinct(UserDevice.userAppId)) > 1)
        .order_by(func.count(func.distinct(UserDevice.userAppId)).desc())
        .limit(limit).all()
    )
    if not groups:
        return []
    ids = [g[0] for g in groups]
    rows = db.query(UserDevice).filter(UserDevice.deviceId.in_(ids)).all()
    users = {u.userAppId: u for u in db.query(User).filter(User.userAppId.in_([r.userAppId for r in rows]))}
    pii = admin.can("pii.read")
    out = []
    for device_id, n in groups:
        members = []
        for r in rows:
            if r.deviceId != device_id:
                continue
            u = users.get(r.userAppId)
            members.append({"uid": int(u.UID) if u else None, "name": u.fullName if u else None,
                            "phone": r.userAppId if pii else mask_tail(r.userAppId),
                            "isVendor": bool(u.alsoVendor) if u else False,
                            "locked": bool(u.lockApp) if u else False, "lastSeenAt": iso(r.lastSeenAt)})
        out.append({"deviceId": mask_tail(device_id, 6), "accounts": int(n),
                    "hasVendor": any(m["isVendor"] for m in members), "members": members})
    return out


# --------------------------------------------------------------------------- notifications


class NotificationIn(BaseModel):
    audience: Literal["all", "vendors", "customers", "phones"]
    phones: list[str] = Field(default_factory=list, max_length=500)
    title: str = Field(min_length=3, max_length=80)
    body: str = Field(min_length=3, max_length=300)
    dryRun: bool = False


def _audience(db: Session, body: NotificationIn):
    q = db.query(User.userAppId, User.fcmToken).filter(
        ~User.userAppId.like("%.DELETED%"), User.lockApp.is_(False), User.fcmToken.isnot(None), User.fcmToken != "")
    if body.audience == "vendors":
        q = q.filter(User.vendorApproved.is_(True))
    elif body.audience == "customers":
        q = q.filter(User.alsoVendor.is_(False))
    elif body.audience == "phones":
        phones = [p.strip() for p in body.phones if re.fullmatch(r"\d{10}", p.strip())]
        if not phones:
            raise HTTPException(status_code=422, detail="NO_VALID_PHONES")
        q = q.filter(User.userAppId.in_(phones))
    return q.all()


def _send_batch(tokens: list[str], title: str, body: str) -> None:
    from ..utils.fcm import send_notification_to_token

    ok = 0
    for t in tokens:
        try:
            res = send_notification_to_token(title=title, body=body, fcm_token=t, url="")
            ok += 1 if (res or {}).get("success", True) else 0
        except Exception:
            continue
    logger.info("admin_broadcast_done sent=%s total=%s", ok, len(tokens))


@router.post("/notifications/send")
def send_notification(body: NotificationIn, request: Request, background: BackgroundTasks,
                      admin: CurrentAdmin = Depends(require("notifications.send")),
                      db: Session = Depends(get_db)) -> dict:
    rows = _audience(db, body)
    if body.dryRun:
        return {"recipients": len(rows), "queued": False}
    if not rows:
        raise HTTPException(status_code=422, detail="AUDIENCE_EMPTY")
    tokens = list({r[1] for r in rows})
    background.add_task(_send_batch, tokens, body.title.strip(), body.body.strip())
    audit(db, admin, request, action="notification.send", entity_type="broadcast", entity_id=body.audience,
          summary=f"{body.title} → {len(rows)} recipients",
          after={"title": body.title, "body": body.body, "audience": body.audience, "recipients": len(rows)},
          commit=True)
    return {"recipients": len(rows), "queued": True}
