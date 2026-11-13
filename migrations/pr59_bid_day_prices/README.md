# PR59 — Per-day prices for sightseeing bids

**Status:** Applied to production `bidapp` on 2026-11-13 (verified: table and column present).

## Purpose

Vendors bidding on a sightseeing request can enter either one total price or a separate price for each day.

| Object | Notes |
|--------|-------|
| `bid_day_prices` | One row per day of a per-day bid (`bidId` = `biddetails.BID`, `dayNo`, `amount` DECIMAL(11,2)) |
| `biddetails.pricingMode` | `VARCHAR(10) NULL`: `PER_DAY` or `TOTAL` for sightseeing bids, NULL for transfers and older bids |

`biddetails.bidAmount` is always the total (the sum of the days for a per-day bid), so bid ranking, wallet commission, final amount and earnings are unchanged. Transfer bids never touch either object. There is no foreign key to `biddetails`; FastAPI deletes a bid's day rows when the vendor deletes the bid or rejects the booking.

## Files

| File | Role |
|------|------|
| `schema_bid_day_prices.sql` | `CREATE TABLE IF NOT EXISTS bid_day_prices` |
| `apply_migration.py` | Dry run by default; `--apply` creates the table and adds `pricingMode` (idempotent). Reuses `../pr50_sightseeing/_db.py` |

## Commands

From `pythonCoding/bidApp/migrations/pr59_bid_day_prices` with `DB_*` set (or `app_v1/.env`). The host's IP must be allowed on the `openbid-mysql` firewall.

```bash
python apply_migration.py            # dry run
python apply_migration.py --apply
```

Order: apply this migration, then deploy FastAPI (it writes `pricingMode`), the worker, and finally the Flutter app.

## Rollback

```sql
DROP TABLE bid_day_prices;
ALTER TABLE biddetails DROP COLUMN pricingMode;
```

Roll back FastAPI first. Existing per-day bids keep their total in `bidAmount`, so they remain valid total-price bids.
