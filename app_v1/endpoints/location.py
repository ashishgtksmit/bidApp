from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from ..crud.location import get_all_locations,get_all_cities,get_all_regions,get_region_city_selections
from ..schemas.location_details import Location,CityDetail,RegionDetail,LocationResponse
from ..schemas.city_list import CityListDetail
from ..schemas.region_details import RegionDetailBase
from ..schemas.location_reports import LocationReportRequest, LocationReportResponse
from ..services.location_reports import LocationReportError, submit_location_report
from ..utils.common import ErrorResponse
from ..auth.deps import AuthenticatedUser, get_current_user
from ..database import get_db
from typing import List, Optional, Union

router = APIRouter()


# @router.get("/getlocations", response_model=List[Location],openapi_extra={"security": [{"BearerAuth": [], "ClientIdHeader": []}]})
@router.get("/getlocations", response_model=Union[List[LocationResponse],ErrorResponse])
def read_locations(
    db:Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
    ):
    user_id = current_user.user_app_id
    return get_all_locations(db)


@router.get("/getcities", response_model=List[CityListDetail])
def read_cities(db:Session = Depends(get_db),
                current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                ):
    user_id = current_user.user_app_id
    return get_all_cities(db)


@router.get("/getregions",response_model=Union[List[RegionDetailBase],ErrorResponse])
def read_regions(db:Session = Depends(get_db)
                 ,current_user: AuthenticatedUser = Depends(get_current_user),  # ⬅️ now protected
                 ):
    user_id = current_user.user_app_id
    return get_all_regions(db)

@router.get("/getuserregionpreferences", response_model=List[RegionDetail])
def read_user_region_preferences(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
    userAppId: Optional[str] = Query(None),
):
    user_id = current_user.user_app_id
    return get_region_city_selections(
        db, user_id=user_id, user_app_id=userAppId
    )


@router.post(
    "/location-reports",
    response_model=LocationReportResponse,
    responses={
        401: {"description": "Missing or invalid JWT"},
        403: {"description": "Account locked / not allowed"},
        404: {"description": "User or region not found"},
        422: {"description": "Validation failure"},
        429: {"description": "Rate limited"},
        500: {"description": "Unexpected failure"},
        503: {"description": "Configuration or SMTP delivery failure"},
    },
)
def create_location_report(
    body: LocationReportRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Submit a missing pickup/drop location report (PR29).

    JWT ``sub`` is the reporter. Recipients, from-address, subject, and HTML
    template are server-owned. Synchronous SMTP; success only after acceptance.
    Does not insert into the location catalog. Not ``POST /sendemail``.
    """
    user_id = current_user.user_app_id
    try:
        outcome = submit_location_report(
            db,
            jwt_sub=user_id,
            location_name=body.locationName,
            landmark=body.landmark,
            region_id=body.regionId,
            region_other=body.regionOther,
            usage_type=body.usageType,
        )
        return LocationReportResponse(message=outcome["message"])
    except LocationReportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LOCATION_REPORT_FAILED",
        ) from None
