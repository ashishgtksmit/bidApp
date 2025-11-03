from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from ..models.location_details import LocationDetail
from ..schemas.location_details import Location,CityDetail,RegionDetail
from ..schemas.user_table import NoUserResponse
from ..models.city_list import City
from ..schemas.city_list import CityListDetail
from ..models.region_details import Region
from ..schemas.region_details import RegionDetailBase
from ..utils.common import ErrorResponse
from ..models.user_table import User
from ..models.region_details import Region


def get_all_locations(db:Session):
    try:
        locations = db.query(LocationDetail).all()
        return [Location.from_orm(loc) for loc in locations]
    except SQLAlchemyError:
        return NoUserResponse(message="ERROR_PREPEARE")

def get_all_cities(db:Session):
    try:
        cities = db.query(City).all()
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

        if not regions:
            return NoUserResponse(message="NO_REGIONS")
        
        return [RegionDetailBase(
            regionId=region_id,
            regionName=region_name
        ) for region_id,region_name in regions]

    except SQLAlchemyError:
        return NoUserResponse(message="ERROR_UPDATE")
    
def get_region_city_selections(db: Session, user_app_id: str):
    if not user_app_id or user_app_id.strip() == "":
        return ErrorResponse(message="ERROR_MISSING_USERAPPID")

    try:
        # Step 1: Get user preferences
        user = db.query(User.regionPreferences, User.cityPreferences).filter(
            User.userAppId == user_app_id
        ).first()

        if not user:
            region_pref_csv, city_pref_csv = "", ""
        else:
            region_pref_csv, city_pref_csv = user.regionPreferences or "", user.cityPreferences or ""

        # Convert CSV to sets
        def to_id_set(csv: str) -> set:
            if not csv:
                return set()
            return {int(tok) for tok in csv.split(",") if tok.strip().isdigit()}

        region_sel = to_id_set(region_pref_csv)
        city_sel = to_id_set(city_pref_csv)

        # Step 2: Load all regions
        regions = {}
        region_results = db.query(Region.RDID, Region.regionName).order_by(
            Region.regionName.asc()
        ).all()

        for rdid, region_name in region_results:
            regions[rdid] = {
                "REGION_ID": rdid,
                "REGION_NAME": region_name,
                "SELECTED": False,
                "CITIES": []
            }

        # Step 3: Load all locations (cities)
        locations = db.query(
            LocationDetail.LID,
            LocationDetail.location,
            LocationDetail.location_shortCode,
            LocationDetail.regionId
        ).order_by(
            LocationDetail.regionId.asc(),
            LocationDetail.location.asc()
        ).all()

        for lid, location, short_code, region_id in locations:
            if region_id not in regions:
                regions[region_id] = {
                    "REGION_ID": region_id,
                    "REGION_NAME": "",
                    "SELECTED": False,
                    "CITIES": []
                }
            regions[region_id]["CITIES"].append({
                "LID": lid,
                "CITY": location,
                "SHORT": short_code,
                "SELECTED": lid in city_sel
            })

        # Step 4: Compute region SELECTED flags
        for region in regions.values():
            region["SELECTED"] = (
                region["REGION_ID"] in region_sel or
                any(city["SELECTED"] for city in region["CITIES"])
            )

        # Step 5: Sort regions by name (case-insensitive)
        sorted_regions = sorted(
            regions.values(),
            key=lambda x: x["REGION_NAME"].lower()
        )

        # Step 6: Convert to schema
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
                        SELECTED=c["SELECTED"]
                    ) for c in r["CITIES"]
                ]
            ) for r in sorted_regions
        ]

    except SQLAlchemyError:
        return ErrorResponse(message="ERROR_PREPARE")
    finally:
        db.close()