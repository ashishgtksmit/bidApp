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
