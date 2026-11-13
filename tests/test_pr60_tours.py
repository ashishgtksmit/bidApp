"""
PR60 Tours: multi-day, city-to-city itineraries on the Tours request type.

In-memory SQLite; reuses the PR51 sightseeing fixtures.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks

from tests.test_pr51_sightseeing_requests import (  # noqa: E402,F401
    CUSTOMER_ID,
    D1,
    D2,
    DARJEELING,
    SS_TYPE_ID,
    _create,
    _insert,
    _seed,
    _sqlite_ids_and_cache,
    engine,
    session_factory,
)
from app_v1.crud import sightseeing as ss  # noqa: E402
from app_v1.crud.bid_day_prices import DayPriceError, validate_day_prices  # noqa: E402
from app_v1.crud.request import create_request, reopen_request  # noqa: E402
from app_v1.models.location_details import LocationDetail  # noqa: E402
from app_v1.models.request_table import Request  # noqa: E402
from app_v1.models.request_type_details import RequestType  # noqa: E402
from app_v1.models.sightseeing import RequestItineraryDay  # noqa: E402

TOURS_TYPE_ID = 4
D3 = D2 + timedelta(days=1)


def _seed_tours(factory, *, name="Tours"):
    s = factory()
    _seed(s)
    s.add(RequestType(RTDID=TOURS_TYPE_ID, requestType=name))
    s.add(LocationDetail(LID=13, location="Bagdogra", regionId=1))
    s.commit()
    s.close()
    return factory


@pytest.fixture()
def tours(session_factory):
    return _seed_tours(session_factory)


def _day(no, day, frm, to, spots, start="07:00", pickup="Hotel X", drop=None):
    body = {
        "dayNo": no,
        "date": day.isoformat(),
        "startTime": start,
        "pickupLandmark": pickup,
        "spotIds": spots,
        "fromLocation": frm,
        "toLocation": to,
    }
    if drop is not None:
        body["dropLandmark"] = drop
    return body


def _tour_days():
    return [
        _day(1, D1, "Gangtok", "Gangtok", [1, 2]),
        _day(2, D2, "Gangtok", "Darjeeling", [], start="09:00", drop="Hotel D"),
        _day(3, D3, "Darjeeling", "Bagdogra", [5], start="06:00", pickup="Hotel D", drop="Bagdogra Airport"),
    ]


def _tour(**overrides):
    body = {
        "requestType": TOURS_TYPE_ID,
        "toLocation": "Bagdogra",
        "toLandmark": "Bagdogra Airport",
        "itinerary": _tour_days(),
    }
    body.update(overrides)
    return _create(**body)


def test_insert_tour_persists_route(tours):
    assert _insert(tours, _tour()).message == "INSERTED"
    s = tours()
    req = s.query(Request).one()
    assert req.requestType == TOURS_TYPE_ID
    assert req.tripEndDate == D3
    days = s.query(RequestItineraryDay).order_by(RequestItineraryDay.dayNo).all()
    assert [(d.fromLocation, d.toLocation, d.dropLandmark) for d in days] == [
        ("Gangtok", "Gangtok", None),
        ("Gangtok", "Darjeeling", "Hotel D"),
        ("Darjeeling", "Bagdogra", "Bagdogra Airport"),
    ]
    loaded = ss.load_itineraries(s, [req.RID])[req.RID]
    assert loaded[1]["FROMLOCATION"] == "Gangtok" and loaded[1]["TOLOCATION"] == "Darjeeling"
    assert loaded[1]["SPOTS"] == []
    assert loaded[2]["DROPLANDMARK"] == "Bagdogra Airport"
    assert [x["NAME"] for x in loaded[2]["SPOTS"]] == ["Tiger Hill"]
    s.close()


def test_city_names_are_canonicalised(tours):
    days = _tour_days()
    days[1]["toLocation"] = " darjeeling "
    days[2]["fromLocation"] = "DARJEELING"
    assert _insert(tours, _tour(itinerary=days)).message == "INSERTED"
    s = tours()
    assert s.query(RequestItineraryDay.toLocation).filter(RequestItineraryDay.dayNo == 2).scalar() == "Darjeeling"
    s.close()


def test_travel_day_may_visit_destination_spots(tours):
    days = _tour_days()
    days[1]["spotIds"] = [5]
    assert _insert(tours, _tour(itinerary=days)).message == "INSERTED"


def test_tours_disabled_until_renamed(session_factory):
    _seed_tours(session_factory, name="Tour")
    assert _insert(session_factory, _tour()).message == "INVALID_ITINERARY"
    s = session_factory()
    assert s.query(Request).count() == 0
    s.close()


def _broken(mutate):
    days = _tour_days()
    mutate(days)
    return days


@pytest.mark.parametrize(
    "days",
    [
        # a single day is sightseeing, not a tour
        [_day(1, D1, "Gangtok", "Bagdogra", [])],
        # day 2 does not start where day 1 ended
        _broken(lambda d: d[1].update(fromLocation="Darjeeling")),
        # unknown city
        _broken(lambda d: d[1].update(toLocation="Atlantis")),
        # missing route
        _broken(lambda d: d[1].pop("toLocation")),
        # local day without spots
        _broken(lambda d: d[0].update(spotIds=[])),
        # spot from a city not on the day's route
        _broken(lambda d: d[0].update(spotIds=[5])),
        # day 1 does not start in the request's fromLocation
        _broken(lambda d: d[0].update(fromLocation="Darjeeling", toLocation="Gangtok")),
        # day 1 pickup must match fromLandmark
        _broken(lambda d: d[0].update(pickupLandmark="Elsewhere")),
        # dates must ascend
        _broken(lambda d: d[2].update(date=D2.isoformat())),
    ],
)
def test_invalid_tours(tours, days):
    assert _insert(tours, _tour(itinerary=days)).message == "INVALID_ITINERARY"
    s = tours()
    assert s.query(Request).count() == 0
    s.close()


def test_last_day_must_end_in_to_location(tours):
    assert _insert(tours, _tour(toLocation="Darjeeling")).message == "INVALID_ITINERARY"


def test_tour_requires_itinerary(tours):
    assert _insert(tours, _tour(itinerary=None)).message == "INVALID_ITINERARY"


def test_tour_max_days(tours, monkeypatch):
    monkeypatch.setenv("TOURS_MAX_DAYS", "2")
    assert _insert(tours, _tour()).message == "INVALID_ITINERARY"
    monkeypatch.delenv("TOURS_MAX_DAYS")
    assert ss.tours_max_days() == 15


def test_sightseeing_day_still_needs_a_spot(tours):
    days = [{"dayNo": 1, "date": D1.isoformat(), "startTime": "07:00", "pickupLandmark": "Hotel X", "spotIds": []}]
    assert _insert(tours, _create(itinerary=days)).message == "INVALID_ITINERARY"


def test_duplicate_check_separates_tours(tours):
    tour = _tour()
    assert _insert(tours, tour).message == "INSERTED"
    transfer = _create(requestType=None, itinerary=None, toLocation="Bagdogra", toLandmark="Bagdogra Airport")
    assert _insert(tours, transfer).message == "INSERTED"
    assert _insert(tours, _tour()).message == "REQUEST_ALREADY_PRESENT"
    assert _insert(tours, transfer).message == "REQUEST_ALREADY_PRESENT"


def test_tour_push_targets_tours_vendors(tours):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]) as vendors:
        result = create_request(tours(), _tour(), BackgroundTasks(), user_id=CUSTOMER_ID, notify=True)
    assert result.message == "INSERTED"
    assert vendors.call_args.kwargs["request_type"] == TOURS_TYPE_ID


def test_sightseeing_push_still_targets_sightseeing(tours):
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]) as vendors:
        create_request(tours(), _create(), BackgroundTasks(), user_id=CUSTOMER_ID, notify=True)
    assert vendors.call_args.kwargs["request_type"] == SS_TYPE_ID


def test_reopen_clones_tour_route(tours):
    assert _insert(tours, _tour()).message == "INSERTED"
    s = tours()
    original = s.query(Request).one()
    original.requestStatus = "BOOKING - CANCELLED BY USER"
    original.bidEndTime = datetime.now() + timedelta(days=2)
    s.commit()
    rid = original.RID
    s.close()
    with patch("app_v1.crud.request.get_vendors_for_request", return_value=[]) as vendors:
        result = reopen_request(tours(), rid, BackgroundTasks(), user_id=CUSTOMER_ID)
    assert result.message == "UPDATED"
    assert vendors.call_args.kwargs["request_type"] == TOURS_TYPE_ID
    s = tours()
    new_row = s.query(Request).filter(Request.RID == result.newRequestId).one()
    assert new_row.requestType == TOURS_TYPE_ID and new_row.tripEndDate == D3
    loaded = ss.load_itineraries(s, [rid, new_row.RID])
    assert loaded[new_row.RID] == loaded[rid]
    s.close()


def test_day_prices_allowed_for_tours(tours):
    assert _insert(tours, _tour()).message == "INSERTED"
    s = tours()
    req = s.query(Request).one()
    amounts = validate_day_prices(s, req, 9000, [3000, 4000, 2000])
    assert [float(a) for a in amounts] == [3000, 4000, 2000]
    with pytest.raises(DayPriceError):
        validate_day_prices(s, req, 9000, [4500, 4500])
    s.close()


def test_tour_type_helpers(tours):
    s = tours()
    assert ss.get_tours_request_type_id(s) == TOURS_TYPE_ID
    assert ss.is_tours_type(s, TOURS_TYPE_ID) and not ss.is_tours_type(s, SS_TYPE_ID)
    assert sorted(ss.itinerary_type_ids(s)) == sorted([SS_TYPE_ID, TOURS_TYPE_ID])
    assert ss.is_itinerary_type(s, TOURS_TYPE_ID) and not ss.is_itinerary_type(s, 1)
    s.close()
