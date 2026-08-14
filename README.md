# OpenBid FastAPI (bidApp)

Python FastAPI backend for the OpenBid marketplace.

## Flutter client transport (PR33 / PR34 notes)

Current OpenBid Flutter **does not** call PHP `entryApi.php`. Production mobile networking uses:

* FastAPI HTTPS (`OpenBidEnvironment.apiBaseUrl` + `OpenBidApiClient`)
* Authenticated WSS (`OpenBidEnvironment.webSocketUrl` + `OpenBidRealtimeService`)
* Firebase RTDB (chat) and FCM (push)

**PR34 (Flutter-only):** shared authenticated request executor introduced for 4 pilot services. **No FastAPI production changes** in PR34. No backend auth-retry was added.

**PR35 (Flutter-only):** the remaining 16 authenticated domain services migrated onto the same shared executor (20 of 20 total). `PushNotificationService.syncFcmTokenToServer` remained excluded/unmigrated at PR35 time. **No FastAPI production changes** in PR35 either — the executor consolidation was entirely client-side.

**PR36:** `PUT /fcmtokenupdate` ownership hardened (JWT `sub` sole authority, JSON body, no `userAppId`) and `PushNotificationService.syncFcmTokenToServer` migrated onto the shared executor via a new `OpenBidFcmTokenService`, closing the PR35 outlier (21 of 21 authenticated domain services now on the shared executor). See **PR36 — FCM Token Sync Hardening** below.

**PR37:** Refresh-token lifecycle hardening — refresh-token-only `POST /refresh`, `sessionVersion`/`accountSessionId` DB identity, JWT session claims, password-reset/deletion revocation, hard HTTP refresh errors, Flutter token-pair v2 + auth-session generation. Migration package: `migrations/pr37_account_session_identity/`. Production apply **not** claimed unless operators run it.

**PR38:** Immutable JWT subject — `authSubjectId` column + migration package `migrations/pr38_immutable_auth_subject/`; newly minted tokens use `sub=authSubjectId` with `identity_version=2` (`JWT_IDENTITY_VERSION_TO_MINT=2` default). Dual-compat accepts PR37 phone-sub while `JWT_ALLOW_LEGACY_PHONE_SUB=true` (default; when `false` → phone-sub → `SESSION_INVALID`). Typed `AuthenticatedUser` dependency; ownership compares use `user_app_id` (phone). `GET /getuserdetails` is queryless; `POST /logout` is JWT-owned. Phone remains the business id (columns / RTDB / worker). Worker/WSS protocol **unchanged**. Production migration apply, compatibility sunset, manual QA, and production telemetry **not** claimed. See `docs/OPENBID_IMMUTABLE_JWT_SUBJECT_PR38_PLAN.md`.

**PR39:** Transactional domain-event outbox + Redis Stream snapshot pipeline (Scope B canary). Successful new `POST /insertbid` may append `bid.created` to `openbid_domain_outbox` in the same MySQL transaction when `DOMAIN_EVENTS_ENABLED=true` **and** `DOMAIN_EVENT_BID_CREATED_ENABLED=true` (both default **false**). Worker `outbox_dispatcher` / `domain_event_consumer` / `snapshot_refresh_processor` publish to Redis Stream, debounce snapshot targets, rebuild via existing fetchers, SET snapshot then PUBLISH `ws_updates`. Canary recipients: request customer + acting bidder only. Does **not** call `request_snapshot_refresh` / `/build_snapshot` from insertbid. Poller `*/30` and prefs/reviews direct refresh unchanged. Flutter/WSS contracts unchanged. Migration package: `migrations/pr39_domain_event_outbox/` — **production apply 2026-08-06** (`AZURE_DB_MIGRATION_APPLY_REPORT_2026-08-06_PR39.md`). Azure Redis Streams verified via private endpoint. See `docs/OPENBID_EVENT_DRIVEN_SNAPSHOT_PIPELINE_PR39_PLAN.md`.

**PR40:** Known-party lifecycle events on the same outbox pipeline: `bid.updated`, `bid.deleted`, `bid.accepted`, `handshake.cancelled`, `handshake.accepted`, `handshake.rejected`, `booking.cancelled_by_customer`, `driver.assignment_changed`. Emission requires master `DOMAIN_EVENTS_ENABLED` **and** the matching `DOMAIN_EVENT_*_ENABLED` flag (code defaults **false**). Recipients are known parties only (no marketplace fan-out). FCM remains FastAPI post-commit for mutations that already send FCM (`bid.updated` intentionally sends **none**). **No new DB migration**. Production Azure Redis remains private-only. **Historical soak acceptance 2026-08-10T10:58:04Z:** Waves **A1/A2/A3 FORMALLY ACCEPTED**. B2 FORMALLY ACCEPTED 2026-08-10T12:31:10Z. **B3 FORMALLY ACCEPTED AFTER CORRECTIVE ACTION** 2026-08-11T05:51:27Z (historical K2 FAIL 2026-08-11T04:58:06Z retained; soak clock unchanged). **Wave B4 canary (2026-08-10):** PASS WITH NON-BLOCKING CAVEAT; **FORMALLY ACCEPTED AFTER CORRECTIVE ACTION** 2026-08-12T12:18:22Z (soak start 2026-08-10T10:13:06.663758Z unchanged). **C1 canary (2026-08-12):** PASS WITH NON-BLOCKING CAVEAT; `DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED=true`; original soak start 2026-08-12T12:54:57.754922Z retained; **PRE-PRODUCTION ACCEPTED — ACCELERATED RISK-BASED POLICY** 2026-08-12T13:29:40.971668Z (48h K2 not claimed). **C2 canary (2026-08-12):** PASS; `DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED=true`; short observation start 2026-08-12T13:33:36.009209Z. See `docs/OPENBID_KNOWN_PARTY_REALTIME_EVENTS_PR40_PLAN.md`.

**PR41 (2026-08-07, Partially Executed; soak acceptance 2026-08-10T10:58:04Z):** Domain-event flag control hardened in deploy **`5939805`**: `load_dotenv(override=False)`, fail-closed `env_flag_enabled`, startup `domain_event_flag_snapshot`, emission decision logs, `tests/test_pr41_domain_event_flag_control.py`. Production matrix A–D passed; poller fallback server-side passed. Current deploy **`47ff010`**. Pass 5: two-Flutter L2 PASS; E9 Android FCM PASS; E11 iOS still open. Waves A1/A2/A3/B2 **FORMALLY ACCEPTED**; B3 **FORMALLY ACCEPTED AFTER CORRECTIVE ACTION** 2026-08-11T05:51:27Z (historical FAIL retained); B4 **FORMALLY ACCEPTED AFTER CORRECTIVE ACTION** 2026-08-12T12:18:22Z; C1 **PRE-PRODUCTION ACCEPTED — ACCELERATED RISK-BASED POLICY** 2026-08-12T13:29:40Z; C2 **PRE-PRODUCTION ACCEPTED — ACCELERATED RISK-BASED POLICY** 2026-08-12T14:34:08Z (canary PASS retained; 48h K2 not claimed). See `docs/OPENBID_EVENT_PIPELINE_PRODUCTION_VALIDATION_PR41_PLAN.md`.

**PR43 (2026-08-13):** `request.created` Connected-First marketplace runtime **IMPLEMENTED** (outbox emission on `POST /insertrequest`, worker preference+presence resolver, Redis WSS presence leases). Flag `DOMAIN_EVENT_REQUEST_CREATED_ENABLED` **true** after attempt 5 canary PASS. Process-bound: `requestCreated.emissionEnabled=true`. No Flutter contract change. See `docs/OPENBID_MARKETPLACE_READINESS_PR42_PLAN.md`.

**PR44 (2026-08-14):** `request.updated` Connected-First marketplace runtime **ENABLED** after U5 canary PASS (outbox on `PUT /updaterequest`). Production `DOMAIN_EVENT_REQUEST_UPDATED_ENABLED=true`. Process-bound: `requestUpdated.emissionEnabled=true`.

**PR45 (2026-08-14):** `request.cancelled` known-party + **CXL1** **ENABLED** after controlled canary PASS (outbox on `DELETE /deleterequest`). Production `DOMAIN_EVENT_REQUEST_CANCELLED_ENABLED=true`. Process-bound: `requestCancelled.perEventEnabled=true`, `emissionEnabled=true`.

**PR46 (2026-08-14):** `request.reopened` Connected-First **ENABLED** after R2 canary PASS (outbox on `PUT /reopenbooking`; NEW RID; no duplicate `request.created`). Production `DOMAIN_EVENT_REQUEST_REOPENED_ENABLED=true`. Process-bound: `requestReopened.emissionEnabled=true`.

**request.* marketplace rollout closure (2026-08-14):** **REQUEST_MARKETPLACE_ROLLOUT_CLOSED=YES**. FastAPI `5a620df`. All four request flags true. No new event added.

**Post-realtime Phase 1 (2026-08-14):** `GET /getallopenbidsforvendor` response mapping fixed in CRUD (`CARRIERREQUEST` + `SPECIALREQUEST`) so valid rows no longer 500 at schema construction. **Not production-deployed** on `5a620df` this pass. Flutter HomePage still does not call this route.

### Client assumption — `POST /refresh`

**PR37:** `/refresh` authenticates with the **refresh token body only**. A valid access JWT is **not** required. Older clients may still send `Authorization`; FastAPI ignores it for refresh ownership. Flutter posts `{ "refresh_token": "..." }` on the raw client with no Bearer header. Access lifetime remains minutes; refresh lifetime uses **days** (`REFRESH_TOKEN_EXPIRE_DAYS`). New tokens carry `session_version` + `session_id` (+ `jti`); legacy claimless refresh tokens are rejected (forced re-login). See `docs/OPENBID_REFRESH_TOKEN_LIFECYCLE_PR37_PLAN.md`.

**PR38:** Refresh still validates refresh token only and remints with PR37 session claims; successful legacy phone-sub refresh converts to `authSubjectId` subject when minting `identity_version=2`.

**WARNING:** Fixing the refresh expiry unit bug changes effective refresh lifetime from approximately **30 minutes** to **30 days** when `REFRESH_TOKEN_EXPIRE_DAYS=30`.

PHP handlers in sibling `bidApp` trees **remain deployed** for old app versions or separate products. **PR33 did not delete PHP handlers** and made **no backend contract change**. Production telemetry is required before any PHP server retirement. PR10/PR11 bid routes remain active FastAPI sources for customer/vendor bidding.

---

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

Authenticated cold-start / session refresh uses `GET /getuserdetails`.

**PR38:** Queryless / JWT-owned — no `userAppId` query; backend resolves via `AuthenticatedUser`. Dual-compat phone-sub tokens still work while `JWT_ALLOW_LEGACY_PHONE_SUB=true`.

**PR23 ownership harden (historical):** When a query `userAppId` was required, it had to equal JWT phone-`sub` (mismatch → **403**). Missing user keeps soft `NO REGISTERED` (compatible with existing bootstrap). Do not use this route to read another user’s profile.

| Field | Semantics |
|-------|-----------|
| `USERAPPID` | Canonical app user id / phone |
| `FULLNAME`, `EMAILID` / `EMAIL`, `DOB`, `CITY`, `GENDER`, `PROFILEPIC` | Profile fields for session rebuild |
| `ALSOVENDOR` / `VENDOR` | Whether the user can operate in vendor mode |
| `CUSTOMERRATING`, `TOTALCUSTOMERRATING` | Passenger / customer ratings |
| `VENDORRATING`, `TOTALVENDORRATING` | Vendor ratings (`null` for non-vendors) |

`RATING` / `TOTALREVIEWS` remain for legacy consumers and mirror **vendor** rating columns. Do **not** overload `RATING` as customer rating when explicit customer/vendor fields are present.

Missing user → `{ "message": "NO REGISTERED" }`.

FCM: `POST /login` persists `fcmToken`; authenticated `PUT /fcmtokenupdate` also runs server-side topic subscription. **PR36 hardened `PUT /fcmtokenupdate` ownership and contract** — see **PR36 — FCM Token Sync Hardening** below.

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

Flutter customer call sites use FastAPI (`OpenBidCustomerBidService`). Vendor bidding/handshake was migrated in PR11 (`OpenBidVendorBidService`). PHP handlers may remain for old clients; current Flutter does not call them (PR33).

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

Flutter: `OpenBidCustomerBookingService` — no PHP fallback on migrated call sites. Lists remain WSS-primary. Worker unchanged (already emits `REQUESTSTATUS` / `REJECTIONREASON` / `REOPENBOOKING` / `REQUESTWONBY` / `FINALAMOUNT`). `RateTheVenor` uses FastAPI PR12/PR19 services (not PHP).

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

## PR16 — Vendor Registration / KYC onboarding

| Endpoint | Behaviour |
|----------|-----------|
| `PUT /registernewvendor` | JWT `sub` authoritative; optional legacy `userAppId` must match or **403**; missing user → **404** `USER_NOT_FOUND`; `lockApp` → **403** `ACCOUNT_LOCKED`; `vendorApproved` → **409** `ALREADY_VENDOR`; customer-only + pending vendor allowed; server forces `alsoVendor=true`; never sets `vendorApproved`; never changes `lockApp`; DOB `yyyy-MM-dd` only; `addressLine2` required; IFSC uppercase; embedded bank fields + private Aadhaar/PAN/bank blobs; `joiningDate` set only if absent; `requestTypePreferences` initialized to `1,2,3,4` only when empty; success `{message: UPDATED}`; KYC email after commit via `KYC_EMAIL_FROM` (fallback `customersupport@wizzride.com`); email failure does not undo commit |

Flutter: `OpenBidVendorOnboardingService` — registration FastAPI-only; no PHP fallback; does **not** call `/alsovendorupdate`. Session refresh via `GET /getuserdetails` + `AppSessionMapper`. WSS remains authoritative for approval/access locks. Post-onboarding bank view/edit migrated in PR17. No worker change.

```bash
python -m pytest tests/test_pr16_vendor_onboarding.py -q
python -m pytest tests/test_pr6_getuserdetails.py tests/test_pr11_vendor_bidding.py \
  tests/test_pr13_vendor_confirmed_trip.py tests/test_pr14_vendor_manage_drivers.py \
  tests/test_pr15_vendor_manage_cars.py -q
```

## PR17 — Vendor Bank Account view / update

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getregisteredbankaccount` | JWT `sub` authoritative; optional legacy `userAppId` must match or **403**; missing user → **404** `USER_NOT_FOUND`; `lockApp` → **403** `ACCOUNT_LOCKED`; customer / pending vendor → **403** `VENDOR_NOT_ELIGIBLE`; approved unlocked vendor → `VendorBankAccountSummaryResponse` (`hasBankAccount`, `maskedAccountNumber`, holder, IFSC, bank name). Never returns full account number, `imageBankAccount`, KYC URLs, or approval flags. Empty bank → typed `hasBankAccount: false` with null fields. |
| `PUT /updatevendorbankdetails` | JWT `sub` authoritative; optional legacy `userAppId` must match or **403**; same eligibility/lock gates; all four bank text fields required; PR16 account/IFSC validation (`ERROR_INVALID_ACCOUNTNO` / `ERROR_INVALID_IFSC`); `SELECT FOR UPDATE` + re-check eligibility; updates only four bank columns + Asia/Kolkata `tableTimestamp` on changed values; preserves `imageBankAccount` and all unrelated fields; same-value replay → `{message: UPDATED}`; no email; no passbook/media; no soft-200 authz errors. |

Flutter: `OpenBidVendorBankService` — FastAPI-only; no PHP fallback; no `userAppId` / `page` / media; 401 refresh-once retry; PUT network/timeout → `unknownCommit` (no auto-retry). PR16 onboarding bank/passbook path unchanged. PHP handlers retained unused. No worker change.

```bash
python -m pytest tests/test_pr17_vendor_bank.py -q
python -m pytest tests/test_pr6_getuserdetails.py tests/test_pr11_vendor_bidding.py \
  tests/test_pr13_vendor_confirmed_trip.py tests/test_pr14_vendor_manage_drivers.py \
  tests/test_pr15_vendor_manage_cars.py tests/test_pr16_vendor_onboarding.py -q
```

## PR18 — Vendor Trip Preferences

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getuserregionpreferences` | JWT `sub` authoritative; optional legacy `userAppId` must match or **403**; missing user → **404** `USER_NOT_FOUND`; `lockApp` → **403** `ACCOUNT_LOCKED`; customer / pending vendor → **403** `VENDOR_NOT_ELIGIBLE`; returns full region/location catalog with `SELECTED` flags (nested city ids are `location_details.LID`). Empty prefs → catalog with all `SELECTED=false` (not `NOT_FOUND`). |
| `GET /getuserrequesttypepreferences` | Same ownership/eligibility; full request-type catalog + `SELECTED`; empty prefs → all false. |
| `PUT /updateregioncityselections` | Body `{regionIds, cityIds}` required int arrays (may be empty); dedupe + ascending CSV; forced master validation (**422** `ERROR_INVALID_REGIONIDS` / `ERROR_INVALID_CITYIDS`); `SELECT FOR UPDATE` + re-check eligibility; atomic replace of both CSV columns; same-value → `{message: UPDATED}` without publish; changed commit then `POST {WORKER_BASE_URL}/build_snapshot` `flag=Vendor`. Does **not** bump `tableTimestamp` (WSS does not use it for prefs). |
| `PUT /updaterequesttypeselections` | Body `{requestTypeIds}` required int array (may be empty); same eligibility/lock/validation (**422** `ERROR_INVALID_REQUESTTYPEIDS`); same-value `{message: UPDATED}`; changed commit then Vendor snapshot refresh. No `NOTHING_TO_UDPATE`. |

Empty city and/or request-type selections mean the vendor receives no matching open requests (not “all”). `regionPreferences` remain stored/UI-only; worker/PR11 continue filtering by city LID + request type only. Notification matching parity gap is unchanged/out of scope.

Flutter: `OpenBidVendorPreferenceService` — FastAPI-only; no PHP fallback; no `userAppId` / `page` / CSV; 401 refresh-once retry; PUT network/timeout → `unknownCommit` (no auto-retry). PHP handlers retained unused.

Env: `WORKER_BASE_URL`, `BUILD_SNAPSHOT_FUNCTION_KEY` (same as openbid-ws). Propagation failure is logged and does not roll back the preference update.

```bash
python -m pytest tests/test_pr18_vendor_preferences.py -q
python -m pytest tests/test_pr6_getuserdetails.py tests/test_pr7_catalogs.py \
  tests/test_pr11_vendor_bidding.py tests/test_pr14_vendor_manage_drivers.py \
  tests/test_pr15_vendor_manage_cars.py tests/test_pr16_vendor_onboarding.py \
  tests/test_pr17_vendor_bank.py -q
```

## PR19 — Reviews & Ratings

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getallreviewsforvendor` | JWT required; public-safe reviews for existing vendor (`VENDORID` query); empty → `[]`; missing vendor → **404** `TARGET_NOT_FOUND`; newest first (`VRID DESC`); no phones/FCM/internal ids |
| `GET /getallreviewsforcustomer` | JWT `sub` only (no `CUSTOMERID`); empty → `[]`; joins rating **giver** (vendor) display fields |
| `POST /insertfeedback` | Customer rates vendor; body `{RID, driverBehaviour, punctuality, carCondition, cleanliness, comments}`; reviewer=JWT; target=`requestWonBy`; eligibility `REQUEST - CONFIRMED` + past Asia/Kolkata pickup; sets `reviewDone=Y`; recalculates vendor aggregate; **201** `{message: INSERTED}`; duplicate → **409** `ALREADY_REVIEWED`; after commit Vendor snapshot refresh |
| `POST /insertcustomerfeedback` | Vendor rates customer; body `{RID, RATING, COMMENTS}`; reviewer=JWT must equal `requestWonBy`; target=`customerAppId`; sets `customerReviewDone=Y`; recalculates customer aggregate; **201** `{message: INSERTED}`; after commit Customer snapshot refresh |

Half-star ratings `0.5`–`5.0`. Decimal migration + unique RID indexes: `migrations/pr19_reviews_ratings_decimal_unique_rid/` (run preflights before apply). Aggregate audit script reports only — no auto repair.

Flutter: `OpenBidReviewService` — FastAPI-only; no PHP fallback; no reviewer/target identity on mutations; 401 refresh-once; POST network/timeout → `unknownCommit`. Own-profile aggregates via `OpenBidUserProfileService` (approach B). PHP handlers retained unused.

```bash
python migrations/pr19_reviews_ratings_decimal_unique_rid/preflight_duplicate_review_rids.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/preflight_numeric_ratings.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/apply_migration.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/audit_aggregates.py
python -m pytest tests/test_pr19_reviews_ratings.py -q
```

## PR20 — Booking History

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getallrequestforuser` | JWT `sub` owns rows (`customerAppId == sub`); past `REQUEST - CONFIRMED` only (Asia/Kolkata); newest pickup first, RID desc tie-break; empty → `[]`; minimized camelCase `CustomerBookingHistoryItem`; optional deprecated `customerAppId` mismatch → **403**; malformed pickup → **500** `HISTORY_DATA_INVALID` |
| `GET /getallconfirmedrequestsforvendor` | JWT `sub` must equal `requestWonBy`; past `REQUEST - CONFIRMED` only; empty → `[]`; minimized camelCase `VendorBookingHistoryItem` including `customerReviewDone` + rating summary + safe car/driver display; optional deprecated `vendorId` mismatch → **403**; no vendor approval/lock gate for reading own history |

Schemas: `app_v1/schemas/booking_history.py`. No pagination. No soft `NO_REQUESTS_FOUND` / SQL leakage. `getbookingreport` unchanged. Cancelled-history hardened in **PR21**. Worker/WSS unchanged.

Flutter: `OpenBidBookingHistoryService` — FastAPI-only; no `customerAppId`/`vendorId`/`page`; 401 refresh-once; no process cache. Screens: `Csr_Booking_History.dart`, `Vendor_Confirmed_History.dart`. My Earnings migrated separately in **PR22** (`GET /vendor/earnings`). Vendor cancellation history → **PR21**. PHP handlers retained.

```bash
python -m pytest tests/test_pr20_booking_history.py -q
```

## PR21 — Vendor Cancellation History

| Endpoint | Behaviour |
|----------|-----------|
| `GET /getallcancelledrequestsforvendor` | JWT `sub` must equal `requestWonBy`; past `BOOKING - CANCELLED BY USER` only (Asia/Kolkata); newest pickup first, RID desc tie-break; empty → `[]`; minimized camelCase `VendorCancelledHistoryItem`; nullable `finalAmount` (null preserved); optional deprecated `vendorId` mismatch → **403**; no vendor approval/lock gate; no CustomerReview/driver/car joins; malformed pickup → **500** `HISTORY_DATA_INVALID`; SQL failure → **500** `HISTORY_QUERY_FAILED` |

WSS Cancelled Trips tab remains responsible for current/future cancellations. Worker unchanged. Flutter: `OpenBidBookingHistoryService.getVendorCancellationHistory()`; screen `Vendor_Cancellation_History.dart`. No PHP fallback. PHP handlers retained. My Earnings migrated separately in **PR22**.

```bash
./bin/pytest tests/test_pr21_vendor_cancellation_history.py -q
./bin/pytest tests/test_pr20_booking_history.py tests/test_pr12_customer_booking_cancellation.py -q
```

## PR22 — Vendor Earnings Report

| Endpoint | Behaviour |
|----------|-----------|
| `GET /vendor/earnings` | JWT `sub` must equal `requestWonBy`; past `REQUEST - CONFIRMED` only (Asia/Kolkata); optional inclusive `startDate`/`endDate` (`yyyy-MM-dd`, both together or neither; max 24 calendar months); empty → **200** zero `VendorEarningsReport`; summary = gross completed booking value from `finalAmount` (INR integers; null→0; negatives excluded); `paymentStatus` ignored; monthly buckets (default last 6 months including current; range = intersecting months with zeros); trips = newest 10 (pickup desc, RID desc); no vendor approval/lock gate; no identity query params; SQL failure → **500** `REPORT_QUERY_FAILED`; range errors → **422** / `REPORT_RANGE_TOO_LARGE` |

Schemas: `app_v1/schemas/vendor_earnings.py`. CRUD: `app_v1/crud/vendor_earnings.py`. Router: `app_v1/endpoints/reporting.py`. Does **not** join bids/reviews/customer. `GET /getbookingreport` unchanged. Worker/WSS/PR20/PR21 unchanged.

Flutter: `OpenBidVendorEarningsService` under `lib/core/earnings/` — FastAPI-only; no `vendorId`/`phone`/`page`; 401 refresh-once; no process cache; no client totals. Screen: `My_Earnings_Page.dart`. Chart visual unchanged (bucket adapter + zero-max guard). No PHP fallback. PHP handlers retained.

```bash
./bin/pytest tests/test_pr22_vendor_earnings.py -q
./bin/pytest tests/test_pr20_booking_history.py tests/test_pr21_vendor_cancellation_history.py -q
```

## PR23 — User Profile Image Upload

| Endpoint | Behaviour |
|----------|-----------|
| `POST /profilepageupload` | JWT `sub` authoritative; optional transitional body `userAppId` must equal JWT when present (mismatch → **403**); JPEG/PNG only; JSON/base64 (raw or data-URI); decoded size ≤ **2 MB** else **413** `PROFILE_IMAGE_TOO_LARGE`; unsupported type → **415** `UNSUPPORTED_PROFILE_IMAGE_TYPE`; malformed/corrupt/MIME mismatch → **422** `INVALID_PROFILE_IMAGE`; missing user → **404** `USER_NOT_FOUND`; tombstone `*.DELETED*` → **403** `PROFILE_UPDATE_NOT_ALLOWED`; `lockApp` alone does **not** block; public Azure blob `{jwt_sub}_profile.{jpg\|png}`; cache-busted URL; response `{message: UPLOADED, url}` only; storage failure → **500** `PROFILE_UPLOAD_FAILED` without DB write; old different-path blob deleted only after successful commit |
| `GET /getuserdetails` | PR6 fields unchanged; **PR23 ownership:** query `userAppId` must equal JWT `sub` (mismatch → **403**); missing user keeps soft `NO REGISTERED` |

Flutter: `OpenBidUserProfileService.uploadProfileImage` — FastAPI-only; no PHP/`page`/`userAppId`/`phone`; 401 refresh-once; timeout → `unknownCommit`; Option B post-upload `getUserDetails` + `AppSessionMapper`; refresh-fail-after-commit local `picture`/`profilepic` fallback. Screen: `My_Profile.dart`. No text-profile editing. Worker/WSS unchanged. PHP handlers retained. Account deletion later migrated in PR24 (separate domain).

```bash
./bin/pytest tests/test_pr23_user_profile.py -q
./bin/pytest tests/test_pr6_getuserdetails.py -q
```

## PR24 — Permanent Account Deletion

| Endpoint | Behaviour |
|----------|-----------|
| `POST /deleteappuser` | JWT `sub` authoritative; body `password` + `deletionReason` (3–500); optional transitional `userAppId` must match JWT (mismatch → **403**); password via `verify_and_update_password` (upgrade stays in same txn); soft tombstone `{id}.DELETED[+n]`; `lockApp=true`; `user_login_status=LOGGEDOUT`; store `deletionReason`; IST `tableTimestamp`; clear DB `fcmToken`; post-commit topic unsubscribe (best effort); lifecycle gates → **409** `DELETION_BLOCKED_*`; wrong password → **409** `WRONG_PASSWORD`; missing/already tombstoned → **404** `USER_NOT_FOUND`; rate limit → **429** `DELETION_RATE_LIMITED`; success `{message: DELETED}` only; no row hard-delete; no related-history rewrite; no blob/chat erase |
| Column length | SQLAlchemy `User.userAppId` → `String(64)`; migration package `migrations/pr24_user_tombstone_identifier_length/` (preflight + idempotent apply). Production apply not claimed by unit tests. |

Flutter: `OpenBidAccountService` + `AppLocalSessionClearer` — FastAPI-only; no PHP/`page`/`userAppId`; 401 refresh-once; timeout/network → `unknownCommit` (clear local session + login; no auto-retry); definite failure reconnects realtime via HomePage callback. Screens: `Permanently_Delete_Account.dart`, `EnterPasswordDialogBox.dart`. Normal HomePage logout unchanged (`AppLogoutService`). Worker unchanged. PHP handlers retained.

```bash
./bin/pytest tests/test_pr24_account_deletion.py -q
./bin/pytest tests/test_pr6_getuserdetails.py tests/test_pr23_user_profile.py -q
```

## PR25 — Notification Dispatch Cleanup / Hardening

Business FCM remains **mutation-owned** (PR8–PR13 helpers). Generic utility routes are **not** mobile business APIs.

| Endpoint | Auth after PR25 |
|----------|-----------------|
| `POST /notificationtodriver` | JWT + `X-OpenBid-Internal-Key` |
| `POST /sendfcmnotification` | JWT + `X-OpenBid-Internal-Key` (raw token; internal only) |
| `POST /sendnotificationtoselecteddrivers` | JWT + `X-OpenBid-Internal-Key` (single canonical route) |
| `POST /sendnotificationtoalldrivers` | JWT + `X-OpenBid-Internal-Key` |
| `POST /sendmarketingnotificationtonumbers` | JWT + `X-OpenBid-Internal-Key` |
| `POST /sendmarketingnotificationtoallusers` | JWT + `X-OpenBid-Internal-Key` |

Env: `INTERNAL_NOTIFICATION_KEY` (fail closed when unset). Header never logged. Ordinary mobile JWT alone → **403** `INTERNAL_NOTIFICATION_ACCESS_REQUIRED`.

Flutter: dead `sendPushNotification*` helpers removed; **no** `lib/core/notifications/` service. Customer↔vendor and support/admin chat push migrated in **PR26/PR27** (`POST /chat/notifications`). Generic routes remain JWT + internal-key. PHP handlers retained. Worker unchanged.

```bash
./bin/pytest tests/test_pr25_notification_dispatch.py -q
./bin/pytest tests/test_pr8_insertrequest.py tests/test_pr9_update_delete_request.py \
  tests/test_pr10_customer_bids.py tests/test_pr11_vendor_bidding.py \
  tests/test_pr12_customer_booking_cancellation.py tests/test_pr13_vendor_confirmed_trip.py -q
```

## PR26 — Chat Push Notification (customer↔vendor)

Dedicated mobile endpoint (not a generic notify utility):

| Endpoint | Auth |
|----------|------|
| `POST /chat/notifications` | JWT + `X-Client-Id` (no internal key) |

Body: `{threadId, messageId}` only (`extra=forbid`). FastAPI reads Firebase RTDB `Chats/{threadId}/{messageId}` via Admin SDK (`FIREBASE_DATABASE_URL` + `FIREBASE_SERVICE_ACCOUNT`), verifies JWT sender, authorizes `customerAppId`/`requestWonBy` for `BID - CONFIRMED` / `REQUEST - CONFIRMED`, derives recipient FCM token + server-owned title/body/`//Chat_Main_Page`, rate-limits/idempotency via `api_rate_limit_buckets`. **PR27** extends the same route for support/admin threads. Chat photo media is **PR28** (`POST /chat/media`). Worker unchanged.

Env (placeholders only):

```
FIREBASE_SERVICE_ACCOUNT=
FIREBASE_DATABASE_URL=https://opnbd-a23e1-default-rtdb.asia-southeast1.firebasedatabase.app
```

```bash
./bin/pytest tests/test_pr26_chat_notifications.py -q
./bin/pytest tests/test_pr25_notification_dispatch.py -q
```

## PR27 — Support Chat Config + Support Push

| Endpoint | Auth | Notes |
|----------|------|-------|
| `GET /chat/support/config` | JWT + `X-Client-Id` | Typed support identity; no FCM/email/KYC |
| `POST /chat/notifications` | JWT + `X-Client-Id` | Classifies peer vs `admin-{phone}` support threads |

**Support identity (Option A — shared account, not a staff role):** exactly one `admin_number` row whose phone matches a live, unlocked, non-tombstoned `usertable` row. Zero or multiple rows → config `available=false`; notification attempts → **503** `SUPPORT_CONFIGURATION_INVALID`. Missing FCM on support does **not** make config unavailable (`NO_TOKEN` on notify).

**Authorization:**

- User→support: `threadId == admin-{jwt_sub}`; RTDB sender=jwt; receiver=configured support; no booking relationship required.
- Support→user: jwt_sub equals configured support; RTDB sender=support; `threadId == admin-{receiver}`; recipient live (tombstone/locked → soft skip).

**Templates (privacy-preserving):** user→support title `New Support Message`; support→user title `OpenBid Support`; fixed bodies (no message text preview). Deep link `//Chat_Main_Page`. Peer PR26 sanitized 80-char preview unchanged.

**Rate limits:** user→support 20/min sender, 15/min pair; support→user 60/min operator, 20/min pair; message event idempotency reuses `chat_notification:{threadId}:{messageId}`.

**Ops:** support phone must map to a normal JWT-capable user with FCM for delivery. Not a multi-agent or explicit support-role system. PHP `getadminno`/`getuserdetails`/`newnotification` unused by migrated Flutter; handlers retained. Chat photo media is **PR28**. Worker unchanged. No DB migration.

```bash
./bin/pytest tests/test_pr27_support_chat.py -q
./bin/pytest tests/test_pr26_chat_notifications.py tests/test_pr25_notification_dispatch.py -q
```

## PR28 — Chat Media Upload (photo)

| Endpoint | Auth | Notes |
|----------|------|-------|
| `POST /chat/media` | JWT + `X-Client-Id` | One PHOTO per request; JSON/base64 data-URI |
| `DELETE /chat/media` | JWT + `X-Client-Id` | Pre-RTDB-commit compensation only |

**Authorization (before image processing where practical):** live unlocked sender; peer thread uses PR26 `customerAppId`/`requestWonBy` allow-list (`BID - CONFIRMED` / `REQUEST - CONFIRMED`) without requiring RTDB message; support uses PR27 identity (`admin-{user}`).

**Limits:** JPEG/PNG only (server re-detects); decoded ≤ 2 MB → **413** `CHAT_MEDIA_TOO_LARGE`; unsupported → **415**; invalid → **422**. No server recompression.

**Storage:** `AZURE_CHAT_DOCS_CONTAINER_URL` + `AZURE_CHAT_DOCS_SAS`. Deterministic path `chat/{sha256(threadId)[:32]}/{messageId}.{jpg|png}` (no raw phones). Durable **public** URL (privacy risk). Metadata digest idempotency (`contentsha256` + uploader/thread hashes). Same content → **200 UPLOADED**; different content → **409** `CHAT_MEDIA_CONFLICT`.

**Cleanup:** Re-authorize; require RTDB message **absent**; delete deterministic blob only; missing blob → **DELETED**; message present → **409** `CHAT_MEDIA_ALREADY_COMMITTED`. Not a user deletion API. Best-effort orphans only.

**Legacy:** `POST /uploadchatdoc` remains deployed and is **not** the mobile contract (prefer `POST /chat/media`). Hotfix (2026-08-06): fixed `time.time` shadow (`datetime.time` import) and single-file body handling so Azure chat-docs uploads work when `AZURE_CHAT_DOCS_*` is configured. PHP `uploadchatdoc` retained unused by migrated Flutter. No worker changes. No DB migration.

Env:

```
AZURE_CHAT_DOCS_CONTAINER_URL=
AZURE_CHAT_DOCS_SAS=
# RATE_LIMIT_CHAT_MEDIA_SENDER_PER_MIN=20
# RATE_LIMIT_CHAT_MEDIA_PAIR_PER_MIN=15
```

```bash
./bin/pytest tests/test_pr28_chat_media.py -q
./bin/pytest tests/test_pr26_chat_notifications.py tests/test_pr27_support_chat.py -q
```

## PR29 — Missing Location Report

| Endpoint | Auth | Notes |
|----------|------|-------|
| `POST /location-reports` | JWT + `X-Client-Id` | CityPointModal uncatalogued pickup/drop reports only |

**Not** `POST /sendemail` (PR31: internal-key restricted + hidden from OpenAPI; PR29 does not depend on it). **Not** a catalog insert.

**Auth / lifecycle:** JWT `sub` loads `usertable`. Missing/tombstoned → **404** `USER_NOT_FOUND`. `lockApp` → **403** `ACCOUNT_LOCKED`. Customer and vendor modes allowed (no `vendorApproved` gate).

**Request** (`extra=forbid`):

```json
{
  "locationName": "…",
  "landmark": "…",
  "regionId": 123,
  "regionOther": false,
  "usageType": "PICKUP"
}
```

Others: `regionId: null`, `regionOther: true`. `usageType` is `PICKUP` or `DROP` only. Reject client phone/userAppId and any email-routing fields.

**Region:** authoritative `regiondetails` lookup → canonical name; unknown id → **404** `REGION_NOT_FOUND`. No location/region table writes.

**Validation:** trim; location 2–120; landmark 2–250; Unicode allowed; reject controls/CRLF/HTML/script/null.

**Email:** env-owned recipients/from; fixed escaped HTML template; subject `OpenBid Missing Location Report — Pickup|Drop` (no phone in subject). Sync SMTP via `utils/email.py`. Success only after SMTP acceptance → `{ "message": "REPORT_SUBMITTED" }`. No BackgroundTasks / outbox / report table.

Env:

```
LOCATION_REPORT_EMAIL_TO=
LOCATION_REPORT_EMAIL_CC=
LOCATION_REPORT_EMAIL_BCC=
LOCATION_REPORT_EMAIL_FROM=customersupport@wizzride.com
# Existing SMTP (required for delivery; never commit real secrets):
# SMTP_CUSTOMERSUPPORT_USERNAME=
# SMTP_CUSTOMERSUPPORT_PASSWORD=
# SMTP_RESERVATIONS_USERNAME=
# SMTP_RESERVATIONS_PASSWORD=
# SMTP_FALLBACK_USERNAME=
# SMTP_FALLBACK_PASSWORD=
```

Missing/invalid `LOCATION_REPORT_EMAIL_TO` or unsupported from → **503** `LOCATION_REPORT_CONFIGURATION_INVALID`. SMTP failure → **503** `LOCATION_REPORT_DELIVERY_FAILED` (no provider text).

**Rate limits** (`api_rate_limit_buckets`; shared helper may fail-open on DB errors): 5/hour/user; 1/day/user+normalized location+canonical region+usageType → **429** `LOCATION_REPORT_RATE_LIMITED`.

```bash
./bin/pytest tests/test_pr29_new_location_report.py -q
./bin/pytest tests/test_pr7_catalogs.py tests/test_pr16_vendor_onboarding.py -q
```

## PR30 — Customer Feedback Dead-Code Cleanup (no new API)

PR30 did **not** add a customer-feedback API.

* Mobile **Contact Us** uses PR27 support chat (`GET /chat/support/config` + RTDB + `POST /chat/notifications`), not email.
* Orphaned Flutter PHP `page: sendemail` client (`Csr_Send_Feedback`) was removed; **no Flutter caller** of PHP or generic FastAPI email remains for customer feedback.
* Dedicated missing-location email remains **`POST /location-reports`** (PR29).
* Generic FastAPI **`POST /sendemail`** is **PR31-restricted**: JWT + `X-OpenBid-Internal-Key`, hidden from OpenAPI, recipient allow-list, fixed sender, plain text, no CC/BCC/attachments, fail-closed rate limits. Ordinary customer/vendor JWT alone → **403**. Still not for mobile clients. Future removal after ~30-day monitoring.
* PHP `sendemail` handlers remain legacy/untrusted and deployed; Flutter-targeted `websocket-servermq` lacks the handler in the checked-in repository; richer PHP trees still contain insecure client-controlled email behavior and hardcoded credential debt.

```bash
# No PR30 backend endpoint tests — regressions only:
./bin/pytest tests/test_pr29_new_location_report.py tests/test_pr27_support_chat.py tests/test_pr26_chat_notifications.py tests/test_pr25_notification_dispatch.py -q
```

## PR31 — Generic Email Route Hardening

| Endpoint | Auth | OpenAPI | Notes |
|----------|------|---------|-------|
| `POST /sendemail` | JWT + `X-Client-Id` + `X-OpenBid-Internal-Key` | **Hidden** | Restricted internal plain-text mail only |

**Not** for ordinary mobile JWTs. Dedicated flows (PR16 / PR29 / car / driver) continue to call in-process `utils.email.send_email` and do **not** use this HTTP route.

**Request** (`InternalEmailSendRequest`, `extra=forbid`):

```json
{
  "purpose": "OPERATIONS",
  "toAddress": "ops@example.com",
  "subject": "…",
  "message": "…"
}
```

`purpose` ∈ `ADMIN_TEST` | `OPERATIONS` | `MIGRATION_COMPAT`. Rejects `fromAddress`, CC/BCC, attachments, templates, HTML flags, identity fields, internal key in body.

**Auth / lifecycle:** Missing/invalid JWT → **401**. Missing/invalid/unset internal key → **403** `INTERNAL_EMAIL_ACCESS_REQUIRED`. Missing/tombstoned user → **404** `USER_NOT_FOUND`. `lockApp` → **403** `ACCOUNT_LOCKED`.

**Recipient policy:** `INTERNAL_EMAIL_ALLOWED_RECIPIENTS` and/or `INTERNAL_EMAIL_ALLOWED_DOMAINS` (comma-separated). Empty both → fail closed **503** `INTERNAL_EMAIL_CONFIGURATION_INVALID`. Disallowed recipient → **403** `INTERNAL_EMAIL_RECIPIENT_NOT_ALLOWED`.

**Sender:** `INTERNAL_EMAIL_FROM` (default `customersupport@wizzride.com`); must be in helper SMTP map. Unsupported → **503** `INTERNAL_EMAIL_CONFIGURATION_INVALID`.

**Content:** subject ≤200, message ≤20 000; reject CR/LF/null/controls in subject; plain text only (`is_html=False`).

**Rate limits** (fail-closed): 5/min, 30/hour, 100/day per hashed caller; 10/hour exact recipient; 20/hour recipient domain; duplicate same caller+recipient+subject+body → **429** `INTERNAL_EMAIL_DUPLICATE_SUPPRESSED` (5 min). Over limit → **429** `INTERNAL_EMAIL_RATE_LIMITED`.

**Success:** `{ "message": "SENT" }`. Delivery/config failures → safe **503** codes (no provider text).

**Helper:** `send_email(..., is_html=True)` default preserved for PR16/PR29/car/driver.

**PHP:** unchanged. **Credentials:** rotate operationally (see checklist in PR31 plan); do not claim rotation completed.

**Future removal:** monitor denied/success traffic ~30 days, migrate any legitimate callers to dedicated endpoints, then remove route in a later approved PR.

## PR36 — FCM Token Sync Hardening

Backend ownership hardening for `PUT /fcmtokenupdate`, closing a client-authoritative IDOR and removing the last Flutter authenticated-executor outlier. **Flutter-only cross-reference:** `PushNotificationService.syncFcmTokenToServer` migrated onto `OpenBidAuthenticatedRequestExecutor` via a new `OpenBidFcmTokenService` — see `docs/FLUTTER_FCM_TOKEN_SYNC_FASTAPI_PR36_PLAN.md`.

| Endpoint | Auth | Notes |
|----------|------|-------|
| `PUT /fcmtokenupdate` | JWT + `X-Client-Id` | JSON body only — no query params |

**Contract:**

```json
{ "fcmToken": "<non-empty token>" }
```

`FcmTokenUpdateRequest` (`extra=forbid`); `fcmToken` required, 1–4096 chars.

**Ownership (fixes a release-blocking IDOR):** JWT `sub` is the sole authority — the endpoint no longer accepts or trusts a client-supplied `userAppId`. Previously the route required a JWT but selected the mutation row by client query `userAppId`, so any authenticated caller could overwrite another live user's stored FCM token. That is now impossible: a caller can only ever set their own row's token.

**Behaviour:**

- Missing / tombstoned user → **404** `USER_NOT_FOUND`.
- Locked (`lockApp=true`) live users are **allowed** to sync — account-lock UX still benefits from reaching the device.
- Same-value replay → **200** `{ "message": "UPDATED" }` with **no** DB write, **no** `tableTimestamp` bump, and **no** forced topic re-subscribe.
- Changed token → updates `User.fcmToken` only; does **not** bump the general `tableTimestamp` (FCM registration is infrastructure state, not business data).
- Topic subscribe (`allusers` + `allvendors` if `alsoVendor`) runs **after** commit, best-effort; a subscribe failure still returns `UPDATED` (never surfaces provider errors to the client).
- Row is locked with `SELECT ... FOR UPDATE` before comparison/write to avoid a logout/update race.

**Errors:** hard **401** (JWT), **404** `USER_NOT_FOUND`, **422** (empty/oversized/invalid token), **429** `FCM_TOKEN_UPDATE_RATE_LIMITED`, **500** `FCM_TOKEN_UPDATE_FAILED`. Soft-200 `FAILED`/`ERROR` bodies are **retired** for this route.

**Rate limiting:** `RATE_LIMIT_FCM_TOKEN_UPDATE_PER_USER` (default **10**) changed-token attempts per `RATE_LIMIT_FCM_TOKEN_UPDATE_WINDOW_SECONDS` (default **3600**) per user, via the shared `api_rate_limit_buckets` helper. Same-value replays do **not** consume the budget. The limiter is **fail-open** on DB errors (existing `enforce_rate_limit` default) — a limiter outage does not block legitimate token registration.

**Logout fix:** `logout_user` CRUD now accepts an `fcm_token` kwarg. The endpoint already called it with `fcm_token=fcmToken`, but the prior CRUD signature had no such parameter — a pre-existing mismatch documented in earlier PR6/PR35 planning. Server-side logout continues to clear the DB token and unsubscribe the previously stored token from topics.

**Not changed:** single-token model (`User.fcmToken` remains one column; no DB migration); Firebase project / service-account configuration; PHP / worker / WSS / RTDB; business FCM (still mutation-owned, PR8–PR13); chat push (PR26/PR27); internal notify routes (PR25).

```bash
./bin/pytest tests/test_pr36_fcm_token_sync.py -q
./bin/pytest tests/test_pr6_getuserdetails.py tests/test_pr24_account_deletion.py -q
```

```bash
./bin/pytest tests/test_pr31_generic_email_security.py -q
./bin/pytest tests/test_pr29_new_location_report.py tests/test_pr16_vendor_onboarding.py -q
```

## PR40 Wave C1 readiness (2026-08-10)

- Process-bound flag snapshot exposes `bookingCancelledByCustomer` (deploy rev `fbd8fd1`).
- Dedicated emission/rollback/FCM tests in `tests/test_pr40_known_party_event_emission.py`.
- Production `DOMAIN_EVENT_BOOKING_CANCELLED_BY_CUSTOMER_ENABLED` later enabled 2026-08-12 after canary PASS WITH NON-BLOCKING CAVEAT; **PRE-PRODUCTION ACCEPTED — ACCELERATED RISK-BASED POLICY** 2026-08-12T13:29:40Z.

## PR40 Wave C2 readiness (2026-08-10)

- Process-bound flag snapshot exposes `driverAssignmentChanged` (deploy rev `47ff010`).
- Dedicated emission/rollback/FCM tests in `tests/test_pr40_known_party_event_emission.py`.
- Production `DOMAIN_EVENT_DRIVER_ASSIGNMENT_CHANGED_ENABLED` later enabled 2026-08-12 after canary PASS; **PRE-PRODUCTION ACCEPTED — ACCELERATED RISK-BASED POLICY** 2026-08-12T14:34:08Z (short observation healthy; 48h K2 not claimed).

