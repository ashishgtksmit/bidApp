from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..models.location_details import LocationDetail
from ..schemas.location_details import CityDetail,RegionDetail,LocationResponse
from ..schemas.user_table import NoUserResponse
from ..models.city_list import City
from ..schemas.city_list import CityListDetail
from ..models.region_details import Region
from ..schemas.region_details import RegionDetailBase
from ..utils.common import ErrorResponse
from ..models.user_table import User


def _reject_user_app_id_mismatch(jwt_sub: str, user_app_id: Optional[str]) -> None:
    if user_app_id is None:
        return
    provided = str(user_app_id).strip()
    if not provided:
        return
    if provided != str(jwt_sub).strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )


def _enforce_approved_vendor_eligibility(user: User) -> None:
    if bool(getattr(user, "lockApp", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ACCOUNT_LOCKED",
        )
    if (
        not bool(getattr(user, "alsoVendor", False))
        or not bool(getattr(user, "vendorApproved", False))
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VENDOR_NOT_ELIGIBLE",
        )


def get_all_locations(db:Session):
    try:
        locations = (
                db.query(
                LocationDetail.LID,
                LocationDetail.location,
                LocationDetail.location_shortCode,
                LocationDetail.regionId,
                Region.regionName
            )
            .outerjoin(Region, LocationDetail.regionId == Region.RDID)
            .order_by(LocationDetail.location.asc())
            .all()
        )
        return [
            LocationResponse(
                LOCATIONCODE=LID,
                LOCATION=location,
                LOCATIONSHORTCODE=location_shortCode,
                REGIONID=regionId,
                REGIONNAME=regionName
            )
        for LID, location, location_shortCode, regionId, regionName in locations
        ]
    except SQLAlchemyError:
        return NoUserResponse(message="ERROR_PREPEARE")

def get_all_cities(db:Session):
    try:
        cities = db.query(City).order_by(City.cities.asc()).all()
        return [CityListDetail(
            CITYID=city.CLID,
            CITY=city.cities,
            STATE=city.state
        ) for city in cities]
    except SQLAlchemyError:
        return NoUserResponse(message="ERROR_PREPARE")
    
def get_all_regions(db:Session):
    try:
        regions = db.query(
            Region.RDID,
            Region.regionName
        ).all()

        return [RegionDetailBase(
            regionId=region_id,
            regionName=region_name
        ) for region_id,region_name in regions]

    except SQLAlchemyError:
        return NoUserResponse(message="ERROR_UPDATE")
    
def get_region_city_selections(
    db: Session,
    user_id: str,
    user_app_id: Optional[str] = None,
):
    """PR18 GET /getuserregionpreferences — JWT-owned catalog + SELECTED flags."""
    jwt_sub = str(user_id).strip()
    _reject_user_app_id_mismatch(jwt_sub, user_app_id)

    try:
        user = (
            db.query(User)
            .filter(User.userAppId == jwt_sub)
            .first()
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="USER_NOT_FOUND",
            )

        _enforce_approved_vendor_eligibility(user)

        region_pref_csv = user.regionPreferences or ""
        city_pref_csv = user.cityPreferences or ""

        def to_id_set(csv: str) -> set:
            if not csv:
                return set()
            out = set()
            for tok in csv.split(","):
                token = tok.strip()
                if token.isdigit():
                    out.add(int(token))
            return out

        region_sel = to_id_set(region_pref_csv)
        city_sel = to_id_set(city_pref_csv)

        regions = {}
        region_results = db.query(Region.RDID, Region.regionName).order_by(
            Region.regionName.asc()
        ).all()

        for rdid, region_name in region_results:
            regions[rdid] = {
                "REGION_ID": rdid,
                "REGION_NAME": region_name or "",
                "SELECTED": False,
                "CITIES": [],
            }

        locations = db.query(
            LocationDetail.LID,
            LocationDetail.location,
            LocationDetail.location_shortCode,
            LocationDetail.regionId,
        ).order_by(
            LocationDetail.regionId.asc(),
            LocationDetail.location.asc(),
        ).all()

        for lid, location, short_code, region_id in locations:
            if region_id not in regions:
                # Orphan location regionId — skip inventing catalog rows.
                continue
            regions[region_id]["CITIES"].append({
                "LID": lid,
                "CITY": location,
                "SHORT": short_code,
                "SELECTED": lid in city_sel,
            })

        for region in regions.values():
            region["SELECTED"] = (
                region["REGION_ID"] in region_sel
                or any(city["SELECTED"] for city in region["CITIES"])
            )

        sorted_regions = sorted(
            regions.values(),
            key=lambda x: x["REGION_NAME"].lower(),
        )

        return [
            RegionDetail(
                REGION_ID=r["REGION_ID"],
                REGION_NAME=r["REGION_NAME"],
                SELECTED=r["SELECTED"],
                CITIES=[
                    CityDetail(
                        LID=c["LID"],
                        CITY=c["CITY"],
                        SHORT=c["SHORT"],
                        SELECTED=c["SELECTED"],
                    )
                    for c in r["CITIES"]
                ],
            )
            for r in sorted_regions
        ]
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to load region preferences",
        ) from None