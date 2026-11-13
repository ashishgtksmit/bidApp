# PR60 — Tours (multi-day, city to city)

**Status:** Columns applied to production 2026-11-13; launch rename not applied.

## Purpose

A Tours request is a normal `requesttable` row whose `requestType` is the `Tours` row of `requesttypedetails` (production RTDID 4, renamed from `Tour` at launch). Its days live in the PR50 itinerary tables. Each tour day can go from one city to another, so the day table gets its own route:

| Column | Notes |
|--------|-------|
| `request_itinerary_days.fromLocation` | `VARCHAR(100) NULL` — city the day starts in (`location_details.location`) |
| `request_itinerary_days.toLocation` | `VARCHAR(100) NULL` — city the day ends in (same as `fromLocation` for a local day) |
| `request_itinerary_days.dropLandmark` | `VARCHAR(200) NULL` — where the day ends (hotel / airport / station) |

Sightseeing days leave all three NULL; their city is `requesttable.fromLocation`. Additive only.

## Files

| File | Role |
|------|------|
| `apply_migration.py` | Dry run by default; `--apply` adds the missing columns (idempotent). Reuses `../pr50_sightseeing/_db.py` |
| `launch_tours.py` | Dry run by default; `--apply` renames RTDID 4 `Tour` → `Tours` (the feature switch) |

## Commands

From `pythonCoding/bidApp/migrations/pr60_tours` with `DB_*` set (or `app_v1/.env`). The host's IP must be allowed on the `openbid-mysql` firewall.

```bash
python apply_migration.py            # dry run
python apply_migration.py --apply
python launch_tours.py               # dry run
python launch_tours.py --apply
```

Order: apply this migration → deploy FastAPI → worker → app build → `launch_tours.py --apply`. FastAPI and the app enable Tours only while a request type is named exactly `Tours`, so nothing is visible before the launch step.

## Rollback

Launch: `UPDATE requesttypedetails SET requestType = 'Tour' WHERE RTDID = 4;` (hides Tours; the API rejects new tours).

Schema (only after rolling back FastAPI and the worker):

```sql
ALTER TABLE request_itinerary_days DROP COLUMN fromLocation, DROP COLUMN toLocation, DROP COLUMN dropLandmark;
```
