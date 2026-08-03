"""PR22 vendor earnings / reporting endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..auth.deps import get_current_user_id
from ..crud.vendor_earnings import get_vendor_earnings_report
from ..database import get_db
from ..schemas.vendor_earnings import VendorEarningsReport

router = APIRouter()


@router.get(
    "/vendor/earnings",
    response_model=VendorEarningsReport,
    summary="Vendor earnings report (PR22)",
    response_description="Gross completed booking value report for the JWT vendor.",
)
def vendor_earnings(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    startDate: Optional[str] = Query(
        None,
        description="Inclusive Asia/Kolkata start date (yyyy-MM-dd). Require with endDate.",
    ),
    endDate: Optional[str] = Query(
        None,
        description="Inclusive Asia/Kolkata end date (yyyy-MM-dd). Require with startDate.",
    ),
) -> VendorEarningsReport:
    """
    JWT-owned vendor earnings report.

    Filters Request.requestWonBy == JWT sub.
    Counts past REQUEST - CONFIRMED pickups only (Asia/Kolkata).
    Metric is gross completed booking value (finalAmount), not paid/net.
    Optional startDate/endDate must both be supplied or both omitted.
    Empty eligible set → 200 zero-valued VendorEarningsReport.
    """
    return get_vendor_earnings_report(
        db,
        user_id=user_id,
        start_date=startDate,
        end_date=endDate,
    )
