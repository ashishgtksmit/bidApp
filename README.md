# OpenBid FastAPI (bidApp)

Python FastAPI backend for the OpenBid marketplace.

## PR5 — Public pre-login auth / OTP / password reset

| Endpoint | Auth | Notes |
|----------|------|-------|
| `GET /checkregistereduser?userAppId=` | Public | `REGISTERED USER` / `NO USERS PRESENT` |
| `POST /otpcall?userAppId=` | Public | Sends SMS; stores **OTP hash** only; response `OTP_SENT` (never returns OTP) |
| `POST /verifyotp` | Public | Body `{ "userAppId", "otp" }`. Success: `{ "message": "OTP_VERIFIED", "reset_token": "..." }` |
| `PUT /updatepassword` | Public + **resetToken required** | Query: `userAppId`, `password`, `resetToken` |
| `POST /insertuser` | Public | Unchanged account creation |

### OTP / reset-token rules

- OTP hash persisted in `otp_challenges` (shared MySQL; not process memory).
- OTP expiry default **5 minutes**; resend replaces previous challenge.
- Verification attempts capped (`OTP_MAX_ATTEMPTS`, default 5) → `OTP_LOCKED`.
- Successful verify **invalidates** the OTP challenge and issues a **short-lived, single-use** `reset_token` bound to `userAppId`.
- `PUT /updatepassword` rejects missing / wrong-user / expired / reused tokens.
- Never log OTP, password, JWT, or reset token.

### Rate limiting

Public check / OTP endpoints use DB table `api_rate_limit_buckets` (multi-instance safe). Tune via env vars in `.env.example`.

### Local tests

```bash
OTP_TEST_BYPASS_SMS=1 OTP_TEST_FIXED_OTP=1234 \
  python -m pytest tests/test_pr5_otp_reset.py -q

python -m pytest tests/test_pr6_getuserdetails.py -q
```

## PR6 — GET /getuserdetails session profile contract

Authenticated cold-start / session refresh uses `GET /getuserdetails?userAppId=`.

| Field | Semantics |
|-------|-----------|
| `USERAPPID` | Canonical app user id / phone |
| `FULLNAME`, `EMAILID` / `EMAIL`, `DOB`, `CITY`, `GENDER`, `PROFILEPIC` | Profile fields for session rebuild |
| `ALSOVENDOR` / `VENDOR` | Whether the user can operate in vendor mode |
| `CUSTOMERRATING`, `TOTALCUSTOMERRATING` | Passenger / customer ratings |
| `VENDORRATING`, `TOTALVENDORRATING` | Vendor ratings (`null` for non-vendors) |

`RATING` / `TOTALREVIEWS` remain for legacy consumers and mirror **vendor** rating columns. Do **not** overload `RATING` as customer rating when explicit customer/vendor fields are present.

Missing user → `{ "message": "NO REGISTERED" }`.

FCM: `POST /login` persists `fcmToken`; authenticated `PUT /fcmtokenupdate` also runs server-side topic subscription.

## PR8 — POST /insertrequest (create request)

| Rule | Behaviour |
|------|-----------|
| Auth | Bearer JWT + `X-Client-Id` |
| Ownership | Body `customerAppId` must equal JWT `sub`; mismatch → **HTTP 403**; persisted id is always JWT `sub` |
| Status | Forced `BID - OPEN` (client `requestStatus` ignored) |
| `requestType` | Defaults to **1** when omitted |
| Duplicate | Open requests only (`requestStatus == "BID - OPEN"`) → `REQUEST_ALREADY_PRESENT` |
| `tableTimestamp` | `Asia/Kolkata` (naive local wall clock stored) |
| Notify | `notify=True`; background task opens its own `SessionLocal()` (not the request-scoped session) |
| Response | `{ "message": "INSERTED" \| "REQUEST_ALREADY_PRESENT" \| "CUSTOMER_NOT_FOUND" \| "ERROR_INSERT" }` — **no RID** |
| Errors | Internal SQL exception strings are not returned to clients |

```bash
python -m pytest tests/test_pr8_insertrequest.py -q
```

## PR9 — PUT /updaterequest + DELETE /deleterequest

| Rule | Behaviour |
|------|-----------|
| Auth | Bearer JWT + `X-Client-Id` |
| Ownership | JWT `sub` must own the request row; wrong owner → **HTTP 403**; missing RID → **HTTP 404**; no mutation |
| Status gate | Both endpoints require `requestStatus == "BID - OPEN"`; else **HTTP 409** `INVALID_REQUEST_STATUS` |
| Update bids | If `noOfBids > 0` → HTTP **200** `{ "message": "NO OF BIDS MORE THAN 0" }` (no field mutation) |
| Update success | HTTP **200** `{ "message": "SUCCESS" }` — editable fields + `specialRequest`; protected lifecycle fields unchanged |
| `tableTimestamp` | Update uses `Asia/Kolkata` (aligned with PR8 create); not updated on validation failures |
| Delete | Soft cancel only: `requestStatus = "REQUEST - CANCELLED BY USER"`; row + bid rows retained |
| Delete success | HTTP **200** `{ "message": "DELETED" }` |
| Notify | `BackgroundTasks` injected; `notify_vendors_request_cancelled(rid)` opens its own `SessionLocal()`; failures logged, do not undo delete |
| Errors | Internal SQL exception strings are not returned to clients |

```bash
python -m pytest tests/test_pr9_update_delete_request.py -q
```

## PR10 — Customer bids / accept / cancel handshake

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getallbidsforrequest?RID=` | JWT owner only; request must be `BID - OPEN`; returns selectable `BID - OPEN` bids sorted by amount; customer-safe schema (**no FCMTOKEN**); empty → `[]` |
| `PUT /acceptbid?RID=&BIDID=` | Identity is RID+BIDID; derives vendor/car/amount from DB; request `BID - OPEN` → `BID - CONFIRMED`; selected bid → `BID - CONFIRMED`; competitors unchanged; does **not** set `requestWonBy`/`finalAmount`; does **not** enforce `bidEndTime`; same BIDID replay idempotent (no duplicate notify); conflicting BIDID → 409 |
| `PUT /cancelhandshakerequest?RID=` | Owner only; `BID - CONFIRMED` → `BID - OPEN` + all bids `BID - OPEN` in one transaction; already `BID - OPEN` → idempotent `CANCELLED`; other statuses → 409; **no FCM** |

Accept winner notification: `notify_vendor_bid_accepted` background task opens its own `SessionLocal()`; failures logged and do not undo commit.

Vendor Flutter call sites for GET bids / insert/update/delete bid / accept/reject handshake remain on PHP.

```bash
python -m pytest tests/test_pr10_customer_bids.py -q
```

## PR11 — Vendor bidding / handshake

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getallbidsforrequestforvendor?RID=` | Active approved vendor; request `BID - OPEN`; open-feed eligibility **or** existing bid; returns `BID - OPEN` bids sorted by amount; **no FCMTOKEN**; empty → `[]`; does **not** weaken customer GET ownership |
| `GET /viewcarsforvendor` | JWT `sub` authoritative; optional `userAppId` must match JWT; admin-approved + not soft-deleted; lean fields; empty → `[]`; Manage Cars uses dedicated PR15 route |
| `POST /insertbid` | Body `RID`/`CARID`/`bidAmount` only; `bidderID`/`bidStatus` from JWT/server; duplicate RID+vendor+CARID → `BID ALREADY PRESENT`; `noOfBids` recomputed; notify after commit (own SessionLocal); **no** `bidEndTime` enforcement |
| `PUT /updatebid?BIDID=` | Body `{bidAmount}`; owner + `BID - OPEN` gates; no FCM; no vehicle change |
| `DELETE /deletebid?BIDID=` | Owner + `BID - OPEN`; hard delete; recompute `noOfBids`; missing → 404; no FCM |
| `PUT /acceptrequestbyvendor?RID=&BIDID=` | JWT vendor + selected confirmed bid; derives `finalAmount`/`requestWonBy`; request → `REQUEST - CONFIRMED`; selected bid → `REQUEST - CONFIRMED`; competitors unchanged; same vendor/BIDID replay idempotent |
| `PUT /rejectrequestbyvendor?RID=&BIDID=` | Body `{rejectionReason}`; reopen request `BID - OPEN`; hard-delete selected bid; recompute `noOfBids`; already open → 409 |

Worker: `vendor_requests` rows include `BIDID` (backward-compatible additive field).

```bash
python -m pytest tests/test_pr11_vendor_bidding.py -q
```

## PR12 — Customer confirmed booking cancel / reopen / vendor details

| Endpoint | Behaviour |
|----------|-----------|
| `PUT /bookingcancelledbyuser?RID=` | Body `{rejectionReason}` only; JWT owner; `REQUEST - CONFIRMED` → `BOOKING - CANCELLED BY USER`; future pickup; trim/validate reason (422); preserve `requestWonBy`/`finalAmount`/bids/driver/payment; idempotent replay → `UPDATED` without re-notify; vendor notify from `requestWonBy` after commit (`Booking Cancelled` / `///Cancelled Trips`; own SessionLocal) |
| `PUT /reopenbooking?RID=` | No body; JWT owner; source `BOOKING - CANCELLED BY USER` + not reopened; future pickup + future `bidEndTime`; atomic clone new `BID - OPEN` RID (same pickup/`bidEndTime`/`specialRequest`); original `requestReopened=1`; response `{message, newRequestId}`; already reopened → 409; notify eligible vendors after commit like create |
| `GET /getvendordetailsbyrid?RID=` | JWT owner; `requestWonBy` + confirmed bid relation; customer-safe `CustomerBookingVendorDetail` (includes `GENDER`; no FCM/KYC/docs/bank); empty → `[]` |

Flutter: `OpenBidCustomerBookingService` — no PHP fallback on migrated call sites. Lists remain WSS-primary. Worker unchanged (already emits `REQUESTSTATUS` / `REJECTIONREASON` / `REOPENBOOKING` / `REQUESTWONBY` / `FINALAMOUNT`). `RateTheVenor` remains PHP.

```bash
python -m pytest tests/test_pr12_customer_booking_cancellation.py -q
```
## PR13 — Vendor confirmed-trip driver list + assignment

| Endpoint | Behaviour |
|----------|-----------|
| `GET /viewdriversforvendor` | Active approved vendor; JWT `sub` authoritative; optional `userAppId` must match JWT; ownership-only driver list; lean `VendorDriverAssignmentSummary` (`DRIVERID`/`DRIVERNAME`/`PHOTO_URL`/optional `DRIVERNUMBER`); empty → `[]`; no KYC/licence/FCM |
| `PUT /updatedrivertorequest` | Body `{RID, DRIVERID}` only; JWT must equal `requestWonBy` and own driver; status gate `REQUEST - CONFIRMED` (else 409); no pickup-time gate; sets `driverAssignedID` + Asia/Kolkata `tableTimestamp`; preserves status/`requestWonBy`/`finalAmount`/vehicle/payment; same-driver replay → `UPDATED` without re-notify; replacement allowed; customer notify after commit (`🚖 Driver Assigned` / `///My Trips`; own SessionLocal) |

Flutter: `OpenBidVendorTripService` — no PHP fallback on migrated assign-dialog methods. Passenger details remain WSS-only. Vendor cancellation not introduced. Worker unchanged.

```bash
python -m pytest tests/test_pr13_vendor_confirmed_trip.py -q
```

## PR14 — Vendor Manage Drivers CRUD + dedicated driver OTP

| Endpoint | Behaviour |
|----------|-----------|
| `GET /viewmanageddriversforvendor` | Active vendor; JWT `sub` only (no `userAppId` query); own active drivers; soft-deleted excluded; newest first; `VendorManagedDriver` (`DRIVERID`/`DRIVERNAME`/`DRIVERNUMBER`/`DRIVERDOB`/`GENDER`/`DRIVERCITY`/`PHOTO_URL`/`ADDEDON`); empty → `[]`; **no** `USERAPPID`/licence/document/FCM |
| `POST /driverotp/send` | Body `{driverPhone, purpose, driverId?}`; purposes `CREATE_DRIVER` / `CHANGE_DRIVER_PHONE`; hashed OTP at rest; never returns OTP; rate-limited; phone-change requires owned driver |
| `POST /driverotp/verify` | Body `{driverPhone, purpose, otp, driverId?}`; success `{message: OTP_VERIFIED, driverOtpToken}` — **not** password-reset `reset_token`; single-use short-lived mutation token |
| `POST /insertnewdriver` | JWT owner (client `userAppId` ignored); requires `CREATE_DRIVER` token; vendor-scoped phone uniqueness; JSON/base64 media; Asia/Kolkata timestamp; response `INSERTED`; token consumed only on commit |
| `POST /updatedriverdetails` | Public `DRIVERID`; editable city/phone/optional photo; phone change requires `CHANGE_DRIVER_PHONE` token bound to vendor+phone+DRIVERID; response `UPDATED` |
| `PUT /deletedriverfromprofile` | Body `{driverId}` only; soft-delete `userAppId=123456789`; active `REQUEST - CONFIRMED` assignment → **409** `DRIVER_ASSIGNED_TO_ACTIVE_TRIP`; replay → 404 |

PR13 lean route stays unchanged. Tables: `driver_otp_challenges`, `driver_otp_tokens` (separate from PR5 reset tokens). Flutter: `OpenBidVendorDriverService` — Manage Drivers FastAPI-only; no PHP fallback.

```bash
OTP_TEST_BYPASS_SMS=1 OTP_TEST_FIXED_OTP=1234 \
  python -m pytest tests/test_pr14_vendor_manage_drivers.py -q
```

## PR15 — Vendor Manage Cars CRUD

| Endpoint | Behaviour |
|----------|-----------|
| `GET /viewmanagedcarsforvendor` | Active vendor; JWT `sub` only; pending + approved; soft-deleted excluded; newest first; `VendorManagedCar` (no USERAPPID/RC/POA/delete internals/FCM); empty → `[]` |
| `GET /getallvendorcartypes` | Active vendor; catalog CTD/manufacturer/model/variant; empty → `[]` |
| `POST /addcartoprofile` | JWT owner; `CreateVendorCarRequest`; `adminApproved=false`; global `normalizedCarRegNo` unique (incl. soft-deleted); POA when owner ≠ vendor `fullName`; JSON/base64 media; Asia/Kolkata `registeredOn`; response `INSERTED`; conflict → 409 `CAR_ALREADY_EXISTS` |
| `PUT /deletecarfromprofile` | Body `{CARID}` only; soft-delete (`isDeleted`/`deletedAt`/`deletedBy`); active bid/request use → **409** `CAR_IN_ACTIVE_USE`; replay → 404 |

PR11 lean `GET /viewcarsforvendor` unchanged (approved + `isDeleted=false`). Bid insert rejects soft-deleted/unapproved/wrong-owner cars. Migration: `migrations/pr15_car_soft_delete_normalized_reg/` (preflight conflict scan required before unique index). Flutter: `OpenBidVendorCarService` — Manage Cars FastAPI-only; no PHP fallback. No edit-car route. No worker change.

```bash
# Preflight (stops on unresolved normalized-registration conflicts):
python migrations/pr15_car_soft_delete_normalized_reg/preflight_normalized_reg_conflicts.py

python -m pytest tests/test_pr15_vendor_manage_cars.py -q
python -m pytest tests/test_pr11_vendor_bidding.py -q
```
