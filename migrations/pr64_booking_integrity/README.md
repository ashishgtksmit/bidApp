# PR64 — Booking integrity (`user_devices`)

Adds `user_devices` so FastAPI can match related parties by install `deviceId`
(alongside phone / `userAppId` checks). Vehicle overlap and related-party
blocks live in `app_v1/services/booking_integrity.py`.

## Apply

```bash
cd pythonCoding/bidApp
python migrations/pr64_booking_integrity/apply_migration.py          # dry run
python migrations/pr64_booking_integrity/apply_migration.py --apply  # execute
```

Requires `DATABASE_URL` (or `DB_*`) like other PR migrations.

## Rollback

Drop is optional and not required for FastAPI rollback:

```sql
DROP TABLE IF EXISTS user_devices;
```
