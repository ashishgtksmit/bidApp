# PR50 — Sightseeing schema and spot seed

**Status:** Migration package implemented. Not yet applied to Azure `bidapp`
(the apply host could not reach `openbid-mysql.mysql.database.azure.com:3306`;
the Azure MySQL firewall must allow the machine running the scripts).

## Purpose

Additive schema for 1-day and multi-day sightseeing requests:

| Object | Notes |
|--------|-------|
| `ob_sightseeing_spots` | OpenBid-owned curated spots per `location_details.LID`; `spotCode` keeps the WizzRide `point_code`; `permitRequired` flag |
| `request_itinerary_days` | One row per day of a sightseeing request (`requestId` = `requesttable.RID`, `dayNo`, `travelDate`, `startTime`, `pickupLandmark`) |
| `request_itinerary_day_spots` | Spots for a day, with `spotNameSnapshot` / `permitRequiredSnapshot` so old bookings are unaffected by catalog edits |
| `requesttable.tripEndDate` | `DATE NULL`; last itinerary day. NULL for transfers |

No foreign keys to legacy tables; FastAPI enforces those relationships.
Transfers never read or write these objects.

## Files

| File | Role |
|------|------|
| `_db.py` | Shared connection + location-name mapping |
| `preflight_sightseeing.py` | Read-only: MySQL version, request types, location mapping, existing objects |
| `build_sightseeing_spot_seed.py` | Offline: builds `seed_sightseeing_spots.sql` + `seed_report.md` from WizzRide migrations 002/021/044 |
| `schema_sightseeing.sql` | `CREATE TABLE IF NOT EXISTS` for the three tables |
| `seed_sightseeing_spots.sql` | Generated; idempotent inserts keyed on `spotCode` |
| `seed_report.md` | Generated; spots per location, skipped/renamed points, items needing review |
| `apply_migration.py` | Dry run by default; `--apply` creates schema, adds column, seeds in one transaction |
| `audit_sightseeing.py` | Read-only counts and integrity checks |
| `launch_sightseeing.py` | PR58 launch: inserts the `Sightseeing` request type and backfills vendor preferences (dry run by default) |

## Commands

From `pythonCoding/bidApp/migrations/pr50_sightseeing` with `DB_*` set (or `app_v1/.env`):

```bash
python build_sightseeing_spot_seed.py      # only when WizzRide sources change
python preflight_sightseeing.py
python apply_migration.py                  # dry run
python apply_migration.py --apply
python audit_sightseeing.py
```

Launch, after FastAPI, the worker and the Flutter release are deployed:

```bash
python launch_sightseeing.py               # dry run: type row + vendors to opt in
python launch_sightseeing.py --apply       # one transaction; prints changed UIDs only
```

Vendors (`alsoVendor = 1`) get the sightseeing id appended to `requestTypePreferences`. Empty preferences become `1,2,3,4,<id>`, the onboarding default. Apps pick up the new type on their next cold start, because request types are cached per process.

Production already had RTDID 3 `Sight Seeing` (unused by requests), so launch renames it to `Sightseeing` instead of inserting a row. The app and API match only the exact name, so the rename is the switch.

Launch rollback: `UPDATE requesttypedetails SET requestType = 'Sight Seeing' WHERE RTDID = 3` (the app hides the toggle and the API rejects new sightseeing requests). Then restore `requestTypePreferences` for the UIDs printed at apply time; vendors that were empty got `1,2,3,4`.

## Rollback

```sql
DROP TABLE request_itinerary_day_spots;
DROP TABLE request_itinerary_days;
DROP TABLE ob_sightseeing_spots;
ALTER TABLE requesttable DROP COLUMN tripEndDate;
```

Only safe before the launch step (no sightseeing requests exist yet).
