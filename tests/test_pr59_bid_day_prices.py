"""
PR59 per-day prices for sightseeing bids.

bidAmount stays the total; optional dayPrices are validated against the
itinerary, stored in bid_day_prices and returned as DAYPRICES. Transfer bids
reject dayPrices. In-memory SQLite.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import test_pr11_vendor_bidding as pr11  # noqa: E402  (sets env + firebase stubs)

from app_v1.crud import sightseeing as ss  # noqa: E402
from app_v1.crud import vendor_bid as vendor_bid_mod  # noqa: E402
from app_v1.crud.bid import get_bids_for_request  # noqa: E402
from app_v1.database import Base  # noqa: E402
from app_v1.models.bid_day_prices import BidDayPrice  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.request_type_details import RequestType  # noqa: E402
from app_v1.models.sightseeing import RequestItineraryDay  # noqa: E402
from app_v1.schemas.bid_details import BidAmountUpdate, VendorBidInsert  # noqa: E402

SS_TYPE_ID = 3
VENDOR_A = pr11.VENDOR_A
VENDOR_B = pr11.VENDOR_B
CUSTOMER_ID = pr11.CUSTOMER_ID


@pytest.fixture(autouse=True)
def _ids_and_cache():
    counters = {Request: 0, RequestItineraryDay: 0, BidDayPrice: 0}
    attrs = {Request: "RID", RequestItineraryDay: "dayId", BidDayPrice: "id"}
    listeners = []
    for model, attr in attrs.items():
        def _assign(mapper, connection, target, _model=model, _attr=attr):
            if getattr(target, _attr, None) is None:
                counters[_model] += 1
                setattr(target, _attr, counters[_model])

        event.listen(model, "before_insert", _assign)
        listeners.append((model, _assign))
    pr11._car_id_seq["n"] = 100
    ss.reset_sightseeing_type_cache()
    try:
        yield
    finally:
        for model, fn in listeners:
            event.remove(model, "before_insert", fn)
        ss.reset_sightseeing_type_cache()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    pr11._prepare_engine(engine)
    Base.metadata.create_all(
        bind=engine,
        tables=[RequestType.__table__, RequestItineraryDay.__table__, BidDayPrice.__table__],
    )
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE biddetails ADD COLUMN pricingMode VARCHAR(10)"))
    session = sessionmaker(bind=engine)()
    pr11._add_user(session, user_app_id=CUSTOMER_ID, uid=1, alsoVendor=False, vendorApproved=False)
    pr11._add_user(session, user_app_id=VENDOR_A, uid=3, full_name="Vendor A", requestTypePreferences=f"1,{SS_TYPE_ID}")
    pr11._add_user(session, user_app_id=VENDOR_B, uid=4, full_name="Vendor B", requestTypePreferences=f"1,{SS_TYPE_ID}")
    pr11._seed_location(session, lid=1, location="Gangtok")
    pr11._seed_location(session, lid=2, location="Siliguri")
    session.add(RequestType(RTDID=1, requestType="Transfers"))
    session.add(RequestType(RTDID=SS_TYPE_ID, requestType="Sightseeing"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _reopen(db):
    return sessionmaker(bind=db.get_bind())()


def _ss_request(db, days: int = 3) -> Request:
    req = pr11._seed_request(db, request_type=SS_TYPE_ID, to_location="Gangtok")
    for n in range(1, days + 1):
        db.add(
            RequestItineraryDay(
                requestId=req.RID,
                dayNo=n,
                travelDate=date(2026, 8, 14 + n),
                startTime=time(7, 0),
                pickupLandmark="Hotel X",
            )
        )
    db.commit()
    return req


def _insert(db, rid, car, amount, day_prices=None):
    return vendor_bid_mod.insert_vendor_bid(
        _reopen(db),
        VendorBidInsert(RID=rid, CARID=car, bidAmount=amount, dayPrices=day_prices),
        user_id=VENDOR_A,
        background_tasks=BackgroundTasks(),
    )


def _bid_row(db, rid):
    return db.execute(
        text("SELECT BID, bidAmount, pricingMode FROM biddetails WHERE rID = :r"), {"r": rid}
    ).fetchone()


def _day_rows(db, bid_id):
    s = _reopen(db)
    rows = (
        s.query(BidDayPrice.dayNo, BidDayPrice.amount)
        .filter(BidDayPrice.bidId == bid_id)
        .order_by(BidDayPrice.dayNo)
        .all()
    )
    s.close()
    return [(int(d), float(a)) for d, a in rows]


# ---------------------------------------------------------------- schema


def test_schema_rejects_non_positive_day_price():
    with pytest.raises(ValidationError):
        VendorBidInsert(RID=1, CARID=1, bidAmount=100, dayPrices=[100, 0])


def test_schema_empty_day_prices_is_total():
    assert VendorBidInsert(RID=1, CARID=1, bidAmount=100, dayPrices=[]).dayPrices is None
    assert BidAmountUpdate(bidAmount=100).dayPrices is None


# ---------------------------------------------------------------- insert


def test_insert_per_day_stores_breakdown_and_total(db):
    req = _ss_request(db)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    res = _insert(db, req.RID, car, 10500, [4000, 3500, 3000])
    assert res.message == "INSERTED"
    bid_id, amount, mode = _bid_row(db, req.RID)
    assert float(amount) == 10500
    assert mode == "PER_DAY"
    assert _day_rows(db, bid_id) == [(1, 4000.0), (2, 3500.0), (3, 3000.0)]


def test_insert_total_on_sightseeing_marks_total(db):
    req = _ss_request(db)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    assert _insert(db, req.RID, car, 9000).message == "INSERTED"
    bid_id, _, mode = _bid_row(db, req.RID)
    assert mode == "TOTAL"
    assert _day_rows(db, bid_id) == []


@pytest.mark.parametrize(
    "amount,days",
    [(10000, [4000, 3500, 3000]), (10500, [4000, 6500])],
    ids=["sum_mismatch", "day_count_mismatch"],
)
def test_insert_invalid_breakdown_rejected(db, amount, days):
    req = _ss_request(db)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    assert _insert(db, req.RID, car, amount, days).message == "INVALID_DAY_PRICES"
    assert _bid_row(db, req.RID) is None


def test_insert_day_prices_on_transfer_rejected(db):
    req = pr11._seed_request(db, request_type=1)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    assert _insert(db, req.RID, car, 2000, [1000, 1000]).message == "INVALID_DAY_PRICES"
    assert _bid_row(db, req.RID) is None


def test_transfer_bid_leaves_pricing_mode_null(db):
    req = pr11._seed_request(db, request_type=1)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    assert _insert(db, req.RID, car, 2000).message == "INSERTED"
    assert _bid_row(db, req.RID)[2] is None


# ---------------------------------------------------------------- update / delete


def test_update_switches_between_modes(db):
    req = _ss_request(db, days=2)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    _insert(db, req.RID, car, 7000, [4000, 3000])
    bid_id = _bid_row(db, req.RID)[0]

    res = vendor_bid_mod.update_vendor_bid(
        _reopen(db), bid_id, BidAmountUpdate(bidAmount=6500), user_id=VENDOR_A
    )
    assert res.message == "UPDATED"
    assert _bid_row(db, req.RID)[1:] == (6500, "TOTAL")
    assert _day_rows(db, bid_id) == []

    res = vendor_bid_mod.update_vendor_bid(
        _reopen(db), bid_id, BidAmountUpdate(bidAmount=6000, dayPrices=[3500, 2500]), user_id=VENDOR_A
    )
    assert res.message == "UPDATED"
    assert _bid_row(db, req.RID)[2] == "PER_DAY"
    assert _day_rows(db, bid_id) == [(1, 3500.0), (2, 2500.0)]


def test_update_invalid_breakdown_keeps_old_bid(db):
    req = _ss_request(db, days=2)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    _insert(db, req.RID, car, 7000, [4000, 3000])
    bid_id = _bid_row(db, req.RID)[0]
    res = vendor_bid_mod.update_vendor_bid(
        _reopen(db), bid_id, BidAmountUpdate(bidAmount=6000, dayPrices=[4000, 3000]), user_id=VENDOR_A
    )
    assert res.message == "INVALID_DAY_PRICES"
    assert float(_bid_row(db, req.RID)[1]) == 7000
    assert _day_rows(db, bid_id) == [(1, 4000.0), (2, 3000.0)]


def test_delete_bid_removes_breakdown(db):
    req = _ss_request(db, days=2)
    car = pr11._seed_car(db, user_app_id=VENDOR_A)
    _insert(db, req.RID, car, 7000, [4000, 3000])
    bid_id = _bid_row(db, req.RID)[0]
    res = vendor_bid_mod.delete_vendor_bid(_reopen(db), bid_id, user_id=VENDOR_A)
    assert res.message == "DELETED"
    assert _day_rows(db, bid_id) == []


# ---------------------------------------------------------------- read


def test_bid_lists_return_day_prices(db):
    req = _ss_request(db, days=2)
    car_a = pr11._seed_car(db, user_app_id=VENDOR_A, reg="SK01A1")
    _insert(db, req.RID, car_a, 7000, [4000, 3000])
    car_b = pr11._seed_car(db, user_app_id=VENDOR_B, reg="SK01B1")
    vendor_bid_mod.insert_vendor_bid(
        _reopen(db),
        VendorBidInsert(RID=req.RID, CARID=car_b, bidAmount=6800),
        user_id=VENDOR_B,
        background_tasks=BackgroundTasks(),
    )

    customer = get_bids_for_request(_reopen(db), req.RID, user_id=CUSTOMER_ID)
    assert [(b.BIDAMOUNT, b.DAYPRICES) for b in customer] == [(6800, None), (7000, [4000.0, 3000.0])]

    vendor = vendor_bid_mod.get_bids_for_request_for_vendor(_reopen(db), req.RID, VENDOR_B)
    assert [(b.BIDAMOUNT, b.DAYPRICES) for b in vendor] == [(6800, None), (7000, [4000.0, 3000.0])]


def test_transfer_bid_list_has_no_day_prices(db):
    req = pr11._seed_request(db, request_type=1)
    pr11._seed_bid(db, rid=req.RID, bidder_id=VENDOR_A, amount=1500)
    bids = get_bids_for_request(_reopen(db), req.RID, user_id=CUSTOMER_ID)
    assert [b.DAYPRICES for b in bids] == [None]
