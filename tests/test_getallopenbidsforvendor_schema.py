"""GET /getallopenbidsforvendor must not 500 on valid request+bid rows."""

from __future__ import annotations

import os
import sys
import types
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
os.environ.setdefault("JWT_ISSUER", "openbid-test")
os.environ.setdefault("JWT_AUDIENCE", "openbid-clients")

_fake_firebase = types.ModuleType("firebase_admin")
_fake_firebase.credentials = types.ModuleType("firebase_admin.credentials")
_fake_firebase.messaging = types.ModuleType("firebase_admin.messaging")
sys.modules.setdefault("firebase_admin", _fake_firebase)
sys.modules.setdefault("firebase_admin.credentials", _fake_firebase.credentials)
sys.modules.setdefault("firebase_admin.messaging", _fake_firebase.messaging)

from app_v1.crud.request import get_all_open_requests_for_vendor  # noqa: E402
from app_v1.schemas.request_table import RequestConfirmedCommonResponse  # noqa: E402


def _req(**kwargs):
    row = MagicMock()
    row.RID = 42
    row.fromLocation = "Guwahati Airport"
    row.fromLandmark = "Arrival"
    row.toLocation = "Shillong"
    row.toLandmark = "Police Bazar"
    row.pickUpDate = date(2026, 8, 20)
    row.pickUpTime = time(10, 0)
    row.noOfAdults = 2
    row.noOfKids = 0
    row.carType = "SUV"
    row.acRequest = True
    row.carrierRequest = True
    row.specialRequest = "Child seat"
    row.bidEndTime = datetime(2026, 8, 19, 18, 0, 0)
    row.requestStatus = "BID - OPEN"
    row.paymentStatus = None
    row.customerAppId = "7022359323"
    row.requestWonBy = None
    row.noOfBids = 1
    row.tableTimestamp = datetime(2026, 8, 14, 12, 0, 0)
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


def test_open_bids_for_vendor_maps_carrierrequest_and_specialrequest():
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.all.return_value = [
        _req()
    ]
    result = get_all_open_requests_for_vendor(db, vendor_id=8637554387)
    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert isinstance(item, RequestConfirmedCommonResponse)
    assert item.CARRIERREQUEST is True
    assert item.SPECIALREQUEST == "Child seat"
    assert item.REQUESTID == 42
    dumped = item.model_dump()
    assert "CARRIERREQUEST" in dumped
    assert "CARRIERREQUES" not in dumped


def test_open_bids_for_vendor_null_specialrequest_is_empty_string():
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.all.return_value = [
        _req(specialRequest=None, carrierRequest=False)
    ]
    result = get_all_open_requests_for_vendor(db, vendor_id=8637554387)
    assert result[0].SPECIALREQUEST == ""
    assert result[0].CARRIERREQUEST is False


def test_crud_source_does_not_use_carrierreques_typo():
    src = (ROOT / "app_v1" / "crud" / "request.py").read_text()
    live = src.split("def get_all_open_requests_for_vendor")[1].split(
        "def get_request_type"
    )[0]
    assert "CARRIERREQUES=" not in live
    assert "CARRIERREQUEST=" in live
    assert "SPECIALREQUEST=" in live
