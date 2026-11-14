# PR63 — Vendor subscription (Rs 500/month)

**Status:** Not applied.

## Purpose

Approved vendors can browse for free; placing or revising bids, adding cars and adding or editing drivers needs an active subscription (enforced only when `VENDOR_SUBSCRIPTION_ENFORCED=1`). Full design: `open_bid/docs/OPENBID_VENDOR_SUBSCRIPTION.md`.

| Object | Notes |
|--------|-------|
| `vendor_subscription_plans` | Plan catalogue. Seeded with `MONTHLY`, 50000 paise, 30 days |
| `vendor_subscription_periods` | One row per paid / renewed / granted period (append-only) |
| `vendor_subscription_state` | One row per vendor: current period end, auto-renew, reminder tracking |
| `vendor_wallet_recharges.purpose` | `VARCHAR(20) NULL`. `NULL` = plain recharge; `SUB:<planCode>` = Cashfree payment for a subscription |

`create_all` creates the three tables at API startup but never adds the `purpose` column, so this migration **must** run before the API is deployed; otherwise every wallet recharge query fails (unknown column).

## Files

| File | Role |
|------|------|
| `schema_vendor_subscription.sql` | `CREATE TABLE IF NOT EXISTS` for the three tables |
| `apply_migration.py` | Dry run by default; `--apply` creates missing tables, adds `purpose`, seeds `MONTHLY` if absent (idempotent). Reuses `../pr50_sightseeing/_db.py` |

## Commands

From `pythonCoding/bidApp/migrations/pr63_vendor_subscription` with `DB_*` set (or `app_v1/.env`). The host's IP must be allowed on the `openbid-mysql` firewall.

```bash
python apply_migration.py            # dry run
python apply_migration.py --apply
```

Order: apply this migration → deploy FastAPI with `VENDOR_SUBSCRIPTION_ENABLED=1`, `VENDOR_SUBSCRIPTION_ENFORCED=0` → deploy the worker (daily timer) → release the app → announce → set `VENDOR_SUBSCRIPTION_ENFORCED=1`.

**The price lives only in the database.** There is no amount in the API or app code; the app shows whatever `GET /vendor/subscription` returns. To change it, either:

- `UPDATE vendor_subscription_plans SET price_paise = 60000 WHERE code = 'MONTHLY';`, or
- `PUT /internal/subscriptions/plans/MONTHLY` with header `X-OpenBid-Wallet-Ops-Key` and body `{"pricePaise": 60000}` (the same endpoint a future admin panel will call).

The new price applies to new purchases and the next auto-renewals immediately. Periods already paid are not changed, and a Cashfree order already opened is charged at the amount it was created with. If the `MONTHLY` row is missing or inactive, purchases return 503 `SUBSCRIPTION_PLAN_NOT_CONFIGURED`.

## Rollback

Set `VENDOR_SUBSCRIPTION_ENFORCED=0` (immediate). To remove completely, roll back FastAPI first, then:

```sql
DROP TABLE vendor_subscription_state;
DROP TABLE vendor_subscription_periods;
DROP TABLE vendor_subscription_plans;
ALTER TABLE vendor_wallet_recharges DROP COLUMN purpose;
```

Dropping the tables loses paid periods; export them first. Subscription money already sits in the wallet ledger as `SUBSCRIPTION` debits and is not affected.
