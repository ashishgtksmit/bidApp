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
```
