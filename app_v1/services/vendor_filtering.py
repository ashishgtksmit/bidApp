# from typing import List, Set
# from sqlalchemy.orm import Session

# from app_v1.models.user_table import User


# # -------------------------------
# # 🔹 Helpers
# # -------------------------------

# def _normalize(value: str) -> str:
#     return str(value or "").strip().lower()


# def _split_csv(value: str) -> List[str]:
#     value = str(value or "").strip()
#     if not value or value.lower() == "null":
#         return []
#     return [v.strip().lower() for v in value.split(",") if v.strip()]


# # -------------------------------
# # 🔹 Core Vendor Fetch
# # -------------------------------

# def get_all_active_vendors(db: Session):
#     """
#     Fetch all vendors (active ones).
#     You can later add filters like:
#     - vendorApproved == True
#     - lockApp == False
#     """
#     return db.query(User).filter(User.alsoVendor == True).all()


# # -------------------------------
# # 🔹 City Preference Matching
# # -------------------------------

# def get_vendors_by_city_preferences(
#     db: Session,
#     from_location: str = "",
#     to_location: str = ""
# ) -> List[str]:
#     """
#     Match vendors based on:
#     - CITYPREFERENCE_NAMES
#     - fallback to vendor CITY
#     """

#     from_city = _normalize(from_location)
#     to_city = _normalize(to_location)

#     vendors = get_all_active_vendors(db)

#     matched_ids: List[str] = []

#     for v in vendors:
#         token = str(getattr(v, "fcmToken", "") or "").strip()
#         if not token or token.lower() == "null":
#             continue

#         # 🔹 get preference names (adjust if your column name differs)
#         pref_raw = getattr(v, "cityPreferenceNames", None)

#         pref_arr = _split_csv(pref_raw)

#         # fallback to vendor city
#         if not pref_arr:
#             vendor_city = _normalize(getattr(v, "city", ""))
#             if not vendor_city:
#                 continue
#             pref_arr = [vendor_city]

#         matches_from = any(city in from_city for city in pref_arr) if from_city else False
#         matches_to = any(city in to_city for city in pref_arr) if to_city else False

#         if matches_from or matches_to:
#             matched_ids.append(str(v.userAppId))

#     return _deduplicate(matched_ids)


# # -------------------------------
# # 🔹 Region Filtering (future-ready)
# # -------------------------------

# def get_vendors_by_region_ids(
#     db: Session,
#     region_ids: List[int]
# ) -> List[str]:
#     """
#     Match vendors based on regionPreferences (CSV stored)
#     """
#     vendors = get_all_active_vendors(db)

#     target_set = set(str(r) for r in region_ids)

#     matched_ids = []

#     for v in vendors:
#         pref_raw = getattr(v, "regionPreferences", None)
#         pref_arr = _split_csv(pref_raw)

#         if any(p in target_set for p in pref_arr):
#             matched_ids.append(str(v.userAppId))

#     return _deduplicate(matched_ids)


# # -------------------------------
# # 🔹 Request-based filtering (MAIN USE CASE)
# # -------------------------------

# def get_vendors_for_request(
#     db: Session,
#     from_location: str,
#     to_location: str
# ) -> List[str]:
#     """
#     Main reusable function for request creation.
#     Currently uses city preference logic.
#     Later can extend with:
#     - region logic
#     - request type logic
#     """

#     return get_vendors_by_city_preferences(
#         db=db,
#         from_location=from_location,
#         to_location=to_location
#     )


# # -------------------------------
# # 🔹 Utility
# # -------------------------------

# def _deduplicate(values: List[str]) -> List[str]:
#     seen: Set[str] = set()
#     result = []

#     for v in values:
#         if v not in seen:
#             result.append(v)
#             seen.add(v)

#     return result


from typing import List, Set, Tuple, Dict
from sqlalchemy.orm import Session

from app_v1.models.user_table import User
from app_v1.models.region_details import Region
from app_v1.models.location_details import LocationDetail
from app_v1.models.request_type_details import RequestType
from app_v1.models.car_details import CarDetail
from app_v1.models.car_type_details import CarTypeDetail
from app_v1.models.bid_details import BidDetail


# ============================================================
# 🔹 BASIC HELPERS
# ============================================================

def _normalize(value: str) -> str:
    return str(value or "").strip().lower()


def _split_csv(value: str) -> List[str]:
    value = str(value or "").strip()
    if not value or value.lower() == "null":
        return []
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def _deduplicate(values: List[str]) -> List[str]:
    seen: Set[str] = set()
    result = []

    for v in values:
        if v not in seen:
            result.append(v)
            seen.add(v)

    return result


# ============================================================
# 🔹 LOOKUP MAPS (REGION / CITY / REQUEST TYPE)
# ============================================================

def _build_vendor_preference_maps(db: Session) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    region_map = {str(r.RDID): r.regionName for r in db.query(Region).all()}
    city_map = {str(c.LID): c.location for c in db.query(LocationDetail).all()}
    request_type_map = {str(r.RTDID): r.requestType for r in db.query(RequestType).all()}

    return region_map, city_map, request_type_map


def _map_preference_names(pref_str: str, lookup_map: dict) -> str:
    if not pref_str:
        return ""

    ids = [x.strip() for x in str(pref_str).split(",") if x.strip()]
    return ", ".join([lookup_map.get(i, i) for i in ids])


# ============================================================
# 🔹 VENDOR FETCH
# ============================================================

def get_all_active_vendors(db: Session):
    return db.query(User).filter(User.alsoVendor == True).all()


def get_all_approved_vendors(db: Session):
    return db.query(User).filter(
        User.alsoVendor == True,
        User.vendorApproved == True
    ).all()


# ============================================================
# 🔹 VENDOR SERIALIZATION (FULL PHP MATCH)
# ============================================================

def serialize_vendor(
    vendor: User,
    region_map: dict,
    city_map: dict,
    request_type_map: dict
) -> dict:

    region_names = _map_preference_names(vendor.regionPreferences, region_map)
    city_names = _map_preference_names(vendor.cityPreferences, city_map)
    request_type_names = _map_preference_names(vendor.requestTypePreferences, request_type_map)

    return {
        "UID": vendor.UID,
        "USERAPPID": vendor.userAppId,
        "PASSWORD": vendor.password,
        "ALTERNATENUMBER": vendor.alternateNumber,
        "FULLNAME": vendor.fullName,
        "EMAILID": vendor.emailId,
        "DOB": vendor.dob,
        "CITY": vendor.city,
        "GENDER": vendor.gender,
        "PROFILEPICTURE": vendor.profilePicture,
        "CUSTOMERRATING": vendor.customerRating,
        "RATING": vendor.rating,
        "TOTALNOOFREVIEWS": vendor.totalNoOfReviews,
        "TOTALCUSTOMERREVIEWS": vendor.totalCustomerReviews,
        "FCMTOKEN": vendor.fcmToken,
        "JOININGDATE": vendor.joiningDate,
        "CUSTSIGNDATE": vendor.custSignUpDate,
        "CUSTNOOFTRIPSCOMPLETED": vendor.custNoOfTripsCompleted,
        "BASELOCATION": vendor.baseLocation,
        "USERLOGINSTATUS": vendor.user_login_status,
        "ALSOVENDOR": vendor.alsoVendor,
        "VENDORAPPROVED": vendor.vendorApproved,
        "LOCKAPP": vendor.lockApp,
        "TAGS": vendor.tags,
        "NOOFTRIPSCOMPLETED": vendor.noOfTripsCompleted,
        "DELETIONREASON": vendor.deletionReason,
        "ADDRESS": vendor.address,
        "STATE": vendor.state,
        "BANKACCOUNTHOLDERNAME": vendor.bankAccountHolderName,
        "BANKACCOUNTNO": vendor.bankAccountNo,
        "BANKIFSC": vendor.bankIFSC,
        "BANKNAME": vendor.bankName,
        "IMAGEAADHAR": vendor.imageAadhar,
        "IMAGEPAN": vendor.imagePAN,
        "IMAGEBANKACCOUNT": vendor.imageBankAccount,

        # Raw preferences
        "REGIONPREFERENCES": vendor.regionPreferences,
        "CITYPREFERENCES": vendor.cityPreferences,
        "REQUESTTYPEPREFERENCES": vendor.requestTypePreferences,

        # Human readable
        "REGIONPREFERENCE_NAMES": region_names,
        "CITYPREFERENCE_NAMES": city_names,
        "REQUESTTYPEPREFERENCENAMES": request_type_names,

        "TABLETIMESTAMP": vendor.tableTimestamp
    }


# ============================================================
# 🔹 ENRICHED VENDOR LISTS
# ============================================================

def get_all_vendors_enriched(db: Session, approved_only: bool = True):

    region_map, city_map, request_type_map = _build_vendor_preference_maps(db)

    if approved_only:
        vendors = get_all_approved_vendors(db)
    else:
        vendors = get_all_active_vendors(db)

    if not vendors:
        return {"message": "NO VENDORS FOUND", "data": []}

    data = [
        serialize_vendor(v, region_map, city_map, request_type_map)
        for v in vendors
    ]

    return {"message": "SUCCESS", "data": data, "total": len(data)}


# ============================================================
# 🔹 FILTERING LOGIC (CORE SYSTEM)
# ============================================================

def get_vendors_by_city_preferences(
    db: Session,
    from_location: str = "",
    to_location: str = ""
) -> List[str]:

    from_city = _normalize(from_location)
    to_city = _normalize(to_location)

    vendors = get_all_active_vendors(db)

    matched_ids: List[str] = []

    for v in vendors:

        token = str(v.fcmToken or "").strip()
        if not token or token.lower() == "null":
            continue

        pref_arr = _split_csv(v.cityPreferences)

        # fallback to vendor city
        if not pref_arr:
            vendor_city = _normalize(v.city)
            if not vendor_city:
                continue
            pref_arr = [vendor_city]

        matches_from = any(city in from_city for city in pref_arr) if from_city else False
        matches_to = any(city in to_city for city in pref_arr) if to_city else False

        if matches_from or matches_to:
            matched_ids.append(str(v.userAppId))

    return _deduplicate(matched_ids)


def get_vendors_by_region_ids(
    db: Session,
    region_ids: List[int]
) -> List[str]:

    vendors = get_all_active_vendors(db)

    target_set = set(str(r) for r in region_ids)

    matched_ids = []

    for v in vendors:
        pref_arr = _split_csv(v.regionPreferences)

        if any(p in target_set for p in pref_arr):
            matched_ids.append(str(v.userAppId))

    return _deduplicate(matched_ids)


# ============================================================
# 🔹 MAIN ENTRY FOR REQUEST-BASED MATCHING
# ============================================================

def get_vendors_for_request(
    db: Session,
    from_location: str,
    to_location: str
) -> List[str]:
    """
    Core reusable function used by:
    - create_request()
    - admin notifications
    - future targeting logic
    """

    return get_vendors_by_city_preferences(
        db=db,
        from_location=from_location,
        to_location=to_location
    )


def get_vendors_by_tags(
    db: Session,
    tag_ids: List[int]
) -> List[str]:
    """
    Match vendors based on tags (stored as CSV in User.tags)
    Example: "1,2,3"
    """

    if not tag_ids:
        return []

    target_set = set(str(t) for t in tag_ids)

    vendors = get_all_active_vendors(db)

    matched_ids = []

    for v in vendors:
        tag_arr = _split_csv(v.tags)

        if any(tag in target_set for tag in tag_arr):
            matched_ids.append(str(v.userAppId))

    return _deduplicate(matched_ids)


def get_vendors_by_rating(
    db: Session,
    min_rating: float = 0.0
) -> List[str]:
    """
    Match vendors above a rating threshold
    """

    vendors = get_all_active_vendors(db)

    matched_ids = []

    for v in vendors:
        try:
            rating = float(v.rating or 0)
        except:
            rating = 0

        if rating >= min_rating:
            matched_ids.append(str(v.userAppId))

    return _deduplicate(matched_ids)


def get_vendors_by_vehicle_type(
    db: Session,
    car_types: List[str]
) -> List[str]:
    """
    Match vendors who own vehicles of given types
    Example: ["SUV", "SEDAN"]
    """

    if not car_types:
        return []

    normalized_types = [t.lower().strip() for t in car_types]

    rows = db.query(
        CarDetail.userAppId,
        CarTypeDetail.car_type
    ).join(
        CarTypeDetail, CarTypeDetail.CTD == CarDetail.CTD
    ).all()

    matched_ids = []

    for user_app_id, car_type in rows:
        if not car_type:
            continue

        if car_type.lower() in normalized_types:
            matched_ids.append(str(user_app_id))

    return _deduplicate(matched_ids)


def get_vendors_advanced(
    db: Session,
    from_location: str = "",
    to_location: str = "",
    tag_ids: List[int] = None,
    min_rating: float = None,
    car_types: List[str] = None
) -> List[str]:
    """
    Combine multiple filters
    Returns intersection of all applied filters
    """

    result_sets = []

    # 🔹 City filter
    if from_location or to_location:
        city_ids = set(get_vendors_by_city_preferences(db, from_location, to_location))
        result_sets.append(city_ids)

    # 🔹 Tags
    if tag_ids:
        tag_set = set(get_vendors_by_tags(db, tag_ids))
        result_sets.append(tag_set)

    # 🔹 Rating
    if min_rating is not None:
        rating_set = set(get_vendors_by_rating(db, min_rating))
        result_sets.append(rating_set)

    # 🔹 Vehicle type
    if car_types:
        vehicle_set = set(get_vendors_by_vehicle_type(db, car_types))
        result_sets.append(vehicle_set)

    # 🔹 No filters → return all vendors
    if not result_sets:
        return [str(v.userAppId) for v in get_all_active_vendors(db)]

    # 🔹 INTERSECTION (important)
    final_set = result_sets[0]
    for s in result_sets[1:]:
        final_set = final_set.intersection(s)

    return list(final_set)


def get_other_vendors_who_bid_on_request(
    db: Session,
    rid: int,
    excluded_vendor_id: str
) -> List[str]:
    rows = (
        db.query(BidDetail.bidderID)
        .filter(
            BidDetail.rID == rid,
            BidDetail.bidderID != excluded_vendor_id
        )
        .distinct()
        .all()
    )

    return _deduplicate([
        str(bidder_id).strip()
        for (bidder_id,) in rows
        if bidder_id
    ])


def get_vendors_who_bid_on_request(db: Session, rid: int) -> List[str]:
    rows = (
        db.query(BidDetail.bidderID)
        .filter(BidDetail.rID == rid)
        .distinct()
        .all()
    )

    return _deduplicate([
        str(bidder_id).strip()
        for (bidder_id,) in rows
        if bidder_id
    ])