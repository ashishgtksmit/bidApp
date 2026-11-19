"""Dashboard, requests, bids and bookings."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.bid_details import BidDetail
from ..models.car_details import CarDetail
from ..models.customer_reviews import CustomerReview
from ..models.driver_details import DriverDetail
from ..models.request_table import Request as TripRequest
from ..models.request_type_details import RequestType
from ..models.sightseeing import RequestItineraryDay, RequestItineraryDaySpot
from ..models.user_table import User
from ..models.vendor_reviews import VendorReview
from ..models.vendor_wallet import VendorWalletRecharge, VendorWalletWithdrawal
from ..models.car_details import CarDetail as _Car
from .common import (
    ALL_STATUSES,
    ST_BOOKING_CANCELLED,
    ST_CONFIRMED,
    ST_HANDSHAKE,
    ST_OPEN,
    ST_REQ_CANCELLED,
    PageParams,
    iso,
    mask_tail,
    to_float,
    today_ist,
)
from .security import CurrentAdmin, audit, require, require_reader

router = APIRouter()

_END = func.coalesce(TripRequest.tripEndDate, TripRequest.pickUpDate)


def _stage(r: TripRequest, today: date) -> str:
    """Human lifecycle stage used by the console."""
    s = r.requestStatus
    if s == ST_OPEN:
        return "open"
    if s == ST_HANDSHAKE:
        return "handshake"
    if s == ST_CONFIRMED:
        end = r.tripEndDate or r.pickUpDate
        return "completed" if end and end < today else "confirmed"
    if s == ST_BOOKING_CANCELLED:
        return "booking_cancelled"
    if s == ST_REQ_CANCELLED:
        return "request_cancelled"
    return "other"


def _type_names(db: Session) -> dict[int, str]:
    return {int(t.RTDID): t.requestType for t in db.query(RequestType).all()}


def _names(db: Session, phones: list[str]) -> dict[str, str]:
    phones = [p for p in phones if p]
    if not phones:
        return {}
    return {u.userAppId: u.fullName for u in db.query(User.userAppId, User.fullName)
            .filter(User.userAppId.in_(phones))}


def _request_row(r: TripRequest, today: date, types: dict, names: dict, pii: bool) -> dict:
    return {
        "rid": int(r.RID),
        "pnr": r.WIZZPNR,
        "from": r.fromLocation,
        "fromLandmark": r.fromLandmark,
        "to": r.toLocation,
        "toLandmark": r.toLandmark,
        "pickupDate": iso(r.pickUpDate),
        "pickupTime": iso(r.pickUpTime),
        "tripEndDate": iso(r.tripEndDate),
        "adults": int(r.noOfAdults or 0),
        "kids": int(r.noOfKids or 0),
        "carType": r.carType,
        "ac": bool(r.acRequest),
        "carrier": bool(r.carrierRequest),
        "specialRequest": r.specialRequest,
        "bidEndTime": iso(r.bidEndTime),
        "status": r.requestStatus,
        "stage": _stage(r, today),
        "requestTypeId": r.requestType,
        "requestType": types.get(int(r.requestType)) if r.requestType else "Transfer",
        "customerPhone": r.customerAppId if pii else mask_tail(r.customerAppId),
        "customerName": names.get(r.customerAppId),
        "vendorPhone": (r.requestWonBy if pii else mask_tail(r.requestWonBy)) if r.requestWonBy else None,
        "vendorName": names.get(r.requestWonBy) if r.requestWonBy else None,
        "finalAmount": int(r.finalAmount or 0),
        "bids": int(r.noOfBids or 0),
        "reopened": bool(r.requestReopened),
        "driverAssigned": r.driverAssignedID is not None,
        "cancellationReason": r.rejectionReason,
        "updatedAt": iso(r.tableTimestamp),
    }


# --------------------------------------------------------------------------- dashboard


@router.get("/dashboard")
def dashboard(
    days: int = Query(30, ge=7, le=180),
    _: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    today = today_ist()
    since = today - timedelta(days=days - 1)
    prev_since = since - timedelta(days=days)

    def count(*conds):
        return db.query(func.count(TripRequest.RID)).filter(*conds).scalar() or 0

    pipeline = {
        "open": count(TripRequest.requestStatus == ST_OPEN, TripRequest.pickUpDate >= today),
        "openExpired": count(TripRequest.requestStatus == ST_OPEN, TripRequest.pickUpDate < today),
        "handshake": count(TripRequest.requestStatus == ST_HANDSHAKE, TripRequest.pickUpDate >= today),
        "confirmed": count(TripRequest.requestStatus == ST_CONFIRMED, _END >= today),
        "completedInPeriod": count(TripRequest.requestStatus == ST_CONFIRMED, _END < today, _END >= since),
    }

    def gmv(start: date, end: date) -> int:
        return int(
            db.query(func.coalesce(func.sum(TripRequest.finalAmount), 0))
            .filter(TripRequest.requestStatus == ST_CONFIRMED, TripRequest.pickUpDate >= start,
                    TripRequest.pickUpDate <= end).scalar() or 0
        )

    period_requests = count(TripRequest.pickUpDate >= since, TripRequest.pickUpDate <= today)
    prev_requests = count(TripRequest.pickUpDate >= prev_since, TripRequest.pickUpDate < since)
    with_bids = count(TripRequest.pickUpDate >= since, TripRequest.pickUpDate <= today, TripRequest.noOfBids > 0)
    period_confirmed = count(TripRequest.requestStatus == ST_CONFIRMED, TripRequest.pickUpDate >= since,
                             TripRequest.pickUpDate <= today)
    prev_confirmed = count(TripRequest.requestStatus == ST_CONFIRMED, TripRequest.pickUpDate >= prev_since,
                           TripRequest.pickUpDate < since)
    avg_bids = db.query(func.avg(TripRequest.noOfBids)).filter(
        TripRequest.pickUpDate >= since, TripRequest.pickUpDate <= today).scalar()

    series_rows = (
        db.query(
            TripRequest.pickUpDate,
            func.count(TripRequest.RID),
            func.sum(case((TripRequest.requestStatus == ST_CONFIRMED, 1), else_=0)),
            func.sum(case((TripRequest.requestStatus == ST_CONFIRMED, TripRequest.finalAmount), else_=0)),
            func.sum(case((TripRequest.requestStatus.in_([ST_BOOKING_CANCELLED, ST_REQ_CANCELLED]), 1), else_=0)),
        )
        .filter(TripRequest.pickUpDate >= since, TripRequest.pickUpDate <= today + timedelta(days=14))
        .group_by(TripRequest.pickUpDate)
        .all()
    )
    by_day = {str(d): (int(n or 0), int(c or 0), int(g or 0), int(x or 0)) for d, n, c, g, x in series_rows}
    series = []
    d = since
    while d <= today + timedelta(days=14):
        n, c, g, x = by_day.get(str(d), (0, 0, 0, 0))
        series.append({"date": d.isoformat(), "requests": n, "confirmed": c, "gmv": g, "cancelled": x,
                       "future": d > today})
        d += timedelta(days=1)

    top_routes = [
        {"from": f, "to": t, "trips": int(n), "gmv": int(g or 0)}
        for f, t, n, g in db.query(TripRequest.fromLocation, TripRequest.toLocation, func.count(TripRequest.RID),
                                   func.sum(TripRequest.finalAmount))
        .filter(TripRequest.requestStatus == ST_CONFIRMED, TripRequest.pickUpDate >= since)
        .group_by(TripRequest.fromLocation, TripRequest.toLocation)
        .order_by(func.count(TripRequest.RID).desc()).limit(6)
    ]
    vend = (
        db.query(TripRequest.requestWonBy, func.count(TripRequest.RID), func.sum(TripRequest.finalAmount))
        .filter(TripRequest.requestStatus == ST_CONFIRMED, TripRequest.pickUpDate >= since,
                TripRequest.requestWonBy.isnot(None))
        .group_by(TripRequest.requestWonBy).order_by(func.sum(TripRequest.finalAmount).desc()).limit(6).all()
    )
    vnames = _names(db, [v[0] for v in vend])
    top_vendors = [{"name": vnames.get(p) or mask_tail(p), "trips": int(n), "gmv": int(g or 0)} for p, n, g in vend]

    live = ~User.userAppId.like("%.DELETED%")
    queues = {
        "vendorApplications": db.query(func.count(User.UID)).filter(
            live, User.alsoVendor.is_(True), User.vendorApproved.is_(False), User.lockApp.is_(False)).scalar() or 0,
        "carsAwaitingApproval": db.query(func.count(_Car.CARID)).filter(
            _Car.adminApproved.is_(False), _Car.isDeleted.is_(False)).scalar() or 0,
        "withdrawalsRequested": db.query(func.count(VendorWalletWithdrawal.id)).filter(
            VendorWalletWithdrawal.status == "REQUESTED").scalar() or 0,
        "withdrawalsRequestedPaise": int(db.query(func.coalesce(func.sum(VendorWalletWithdrawal.amount_paise), 0))
                                         .filter(VendorWalletWithdrawal.status == "REQUESTED").scalar() or 0),
        "rechargesNeedingReview": db.query(func.count(VendorWalletRecharge.id)).filter(
            VendorWalletRecharge.ops_alerted_at.isnot(None),
            VendorWalletRecharge.status != "PAID").scalar() or 0,
        "openWithoutBids": count(TripRequest.requestStatus == ST_OPEN, TripRequest.pickUpDate >= today,
                                 TripRequest.noOfBids == 0),
        "confirmedWithoutDriver": count(TripRequest.requestStatus == ST_CONFIRMED, TripRequest.pickUpDate >= today,
                                        TripRequest.pickUpDate <= today + timedelta(days=2),
                                        TripRequest.driverAssignedID.is_(None)),
    }
    users = {
        "customers": db.query(func.count(User.UID)).filter(live).scalar() or 0,
        "vendorsApproved": db.query(func.count(User.UID)).filter(live, User.vendorApproved.is_(True),
                                                                 User.lockApp.is_(False)).scalar() or 0,
        "locked": db.query(func.count(User.UID)).filter(live, User.lockApp.is_(True)).scalar() or 0,
    }
    return {
        "periodDays": days,
        "asOf": today.isoformat(),
        "pipeline": pipeline,
        "kpis": {
            "gmv": gmv(since, today),
            "gmvPrev": gmv(prev_since, since - timedelta(days=1)),
            "requests": period_requests,
            "requestsPrev": prev_requests,
            "confirmed": period_confirmed,
            "confirmedPrev": prev_confirmed,
            "bidCoverage": round(with_bids / period_requests, 3) if period_requests else None,
            "avgBidsPerRequest": round(float(avg_bids), 2) if avg_bids is not None else None,
        },
        "series": series,
        "topRoutes": top_routes,
        "topVendors": top_vendors,
        "queues": queues,
        "users": users,
    }


# --------------------------------------------------------------------------- requests


def _filtered_requests(db: Session, *, stage: Optional[str], request_type: Optional[int], q: Optional[str],
                       date_from: Optional[date], date_to: Optional[date]):
    today = today_ist()
    query = db.query(TripRequest)
    if stage == "open":
        query = query.filter(TripRequest.requestStatus == ST_OPEN, TripRequest.pickUpDate >= today)
    elif stage == "expired":
        query = query.filter(TripRequest.requestStatus == ST_OPEN, TripRequest.pickUpDate < today)
    elif stage == "handshake":
        query = query.filter(TripRequest.requestStatus == ST_HANDSHAKE)
    elif stage == "confirmed":
        query = query.filter(TripRequest.requestStatus == ST_CONFIRMED, _END >= today)
    elif stage == "completed":
        query = query.filter(TripRequest.requestStatus == ST_CONFIRMED, _END < today)
    elif stage == "cancelled":
        query = query.filter(TripRequest.requestStatus.in_([ST_BOOKING_CANCELLED, ST_REQ_CANCELLED]))
    if request_type is not None:
        if request_type == 1:
            query = query.filter(or_(TripRequest.requestType.is_(None), TripRequest.requestType == 1))
        else:
            query = query.filter(TripRequest.requestType == request_type)
    if date_from:
        query = query.filter(TripRequest.pickUpDate >= date_from)
    if date_to:
        query = query.filter(TripRequest.pickUpDate <= date_to)
    if q:
        s = q.strip()
        like = f"%{s}%"
        conds = [TripRequest.fromLocation.like(like), TripRequest.toLocation.like(like),
                 TripRequest.customerAppId.like(like), TripRequest.requestWonBy.like(like),
                 TripRequest.WIZZPNR.like(like)]
        if s.isdigit():
            conds.append(TripRequest.RID == int(s))
        query = query.filter(or_(*conds))
    return query


_STAGE_PATTERN = "^(all|open|expired|handshake|confirmed|completed|cancelled)$"


@router.get("/requests")
def list_requests(
    stage: str = Query("all", pattern=_STAGE_PATTERN),
    requestType: Optional[int] = Query(None, ge=1),
    q: Optional[str] = Query(None, max_length=100),
    dateFrom: Optional[date] = None,
    dateTo: Optional[date] = None,
    sort: str = Query("pickup_desc", pattern="^(pickup_desc|pickup_asc|rid_desc|amount_desc)$"),
    pg: PageParams = Depends(),
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    query = _filtered_requests(db, stage=None if stage == "all" else stage, request_type=requestType, q=q,
                               date_from=dateFrom, date_to=dateTo)
    order = {
        "pickup_desc": (TripRequest.pickUpDate.desc(), TripRequest.pickUpTime.desc(), TripRequest.RID.desc()),
        "pickup_asc": (TripRequest.pickUpDate.asc(), TripRequest.pickUpTime.asc()),
        "rid_desc": (TripRequest.RID.desc(),),
        "amount_desc": (TripRequest.finalAmount.desc(),),
    }[sort]
    rows, total = pg.apply(query.order_by(*order))
    today = today_ist()
    types = _type_names(db)
    names = _names(db, [r.customerAppId for r in rows] + [r.requestWonBy for r in rows])
    pii = admin.can("pii.read")
    return pg.wrap([_request_row(r, today, types, names, pii) for r in rows], total)


@router.get("/requests/export.csv")
def export_requests(
    request: Request,
    stage: str = Query("all", pattern=_STAGE_PATTERN),
    requestType: Optional[int] = Query(None, ge=1),
    q: Optional[str] = Query(None, max_length=100),
    dateFrom: Optional[date] = None,
    dateTo: Optional[date] = None,
    admin: CurrentAdmin = Depends(require("export")),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    if not dateFrom or not dateTo or (dateTo - dateFrom).days > 366:
        raise HTTPException(status_code=422, detail="EXPORT_RANGE_REQUIRED_MAX_366_DAYS")
    rows = _filtered_requests(db, stage=None if stage == "all" else stage, request_type=requestType, q=q,
                              date_from=dateFrom, date_to=dateTo).order_by(TripRequest.pickUpDate).limit(20000).all()
    today = today_ist()
    types = _type_names(db)
    names = _names(db, [r.customerAppId for r in rows] + [r.requestWonBy for r in rows])
    buf = io.StringIO()
    cols = ["rid", "pnr", "requestType", "stage", "status", "from", "to", "pickupDate", "pickupTime",
            "tripEndDate", "adults", "kids", "carType", "bids", "finalAmount", "customerName", "customerPhone",
            "vendorName", "vendorPhone", "driverAssigned", "cancellationReason"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(_request_row(r, today, types, names, True))
    audit(db, admin, request, action="export.requests", entity_type="report",
          summary=f"{len(rows)} rows {dateFrom}..{dateTo} stage={stage}", commit=True)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="openbid-{stage}-{dateFrom}-{dateTo}.csv"'})


@router.get("/requests/{rid}")
def request_detail(
    rid: int,
    admin: CurrentAdmin = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict:
    r = db.query(TripRequest).filter(TripRequest.RID == rid).first()
    if r is None:
        raise HTTPException(status_code=404, detail="REQUEST_NOT_FOUND")
    today = today_ist()
    pii = admin.can("pii.read")
    bids = db.query(BidDetail).filter(BidDetail.rID == rid).order_by(BidDetail.bidAmount.asc()).all()
    phones = [r.customerAppId, r.requestWonBy] + [str(b.bidderID) for b in bids]
    names = _names(db, phones)
    out = _request_row(r, today, _type_names(db), names, pii)
    cars = {int(c.CARID): c for c in db.query(CarDetail).filter(
        CarDetail.CARID.in_([int(b.CARID) for b in bids if b.CARID] or [0]))}
    ratings = {u.userAppId: to_float(u.rating) for u in db.query(User.userAppId, User.rating)
               .filter(User.userAppId.in_([str(b.bidderID) for b in bids] or [""]))}
    out["bidList"] = [
        {
            "bidId": int(b.BID),
            "vendorName": names.get(str(b.bidderID)),
            "vendorPhone": str(b.bidderID) if pii else mask_tail(str(b.bidderID)),
            "vendorRating": ratings.get(str(b.bidderID)),
            "amount": to_float(b.bidAmount),
            "status": b.bidStatus,
            "car": None if not b.CARID or int(b.CARID) not in cars else {
                "carId": int(b.CARID), "regNo": cars[int(b.CARID)].carRegNo, "model": cars[int(b.CARID)].carModel,
            },
            "won": bool(r.requestWonBy and str(b.bidderID) == str(r.requestWonBy)
                        and r.requestStatus in (ST_HANDSHAKE, ST_CONFIRMED)),
            "updatedAt": iso(b.last_updated),
        }
        for b in bids
    ]
    days = db.query(RequestItineraryDay).filter(RequestItineraryDay.requestId == rid).order_by(
        RequestItineraryDay.dayNo).all()
    spots = {}
    if days:
        for s in db.query(RequestItineraryDaySpot).filter(
                RequestItineraryDaySpot.dayId.in_([d.dayId for d in days])).order_by(RequestItineraryDaySpot.sequenceNo):
            spots.setdefault(s.dayId, []).append({"name": s.spotNameSnapshot, "permit": bool(s.permitRequiredSnapshot)})
    out["itinerary"] = [
        {"day": d.dayNo, "date": iso(d.travelDate), "startTime": iso(d.startTime), "pickup": d.pickupLandmark,
         "from": d.fromLocation, "to": d.toLocation, "drop": d.dropLandmark, "spots": spots.get(d.dayId, [])}
        for d in days
    ]
    driver = None
    if r.driverAssignedID:
        dd = db.query(DriverDetail).filter(DriverDetail.DDID == r.driverAssignedID).first()
        if dd:
            driver = {"name": dd.driverName, "phone": dd.driverNumber if pii else mask_tail(dd.driverNumber),
                      "photo": dd.driverPhoto}
    out["driver"] = driver
    vr = db.query(VendorReview).filter(VendorReview.RID == rid).first()
    cr = db.query(CustomerReview).filter(CustomerReview.RID == str(rid)).first()
    out["reviews"] = {
        "ofVendor": None if vr is None else {
            "driverBehaviour": to_float(vr.driverBehaviour), "punctuality": to_float(vr.punctuality),
            "carCondition": to_float(vr.carCondition), "cleanliness": to_float(vr.cleanliness),
            "comments": vr.comments},
        "ofCustomer": None if cr is None else {"rating": to_float(cr.generalRating), "comments": cr.comments},
    }
    return out
