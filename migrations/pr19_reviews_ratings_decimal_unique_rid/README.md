# PR19 Reviews & Ratings — Database Migration

**Status:** Scripts ready. Azure `bidapp` apply **blocked 2026-08-04** by
duplicate `vendorreviews.RID` groups (RID 34, 44) — see
`migrations/AZURE_DB_MIGRATION_APPLY_REPORT_2026-08-04.md`.  
**Directory:** `migrations/pr19_reviews_ratings_decimal_unique_rid/`

## Purpose

1. Convert vendor category rating columns to `DECIMAL(2,1)` so half-star values persist.
2. Convert `customerreviews.generalRating` to `DECIMAL(2,1)`.
3. Add unique indexes on `vendorreviews.RID` and `customerreviews.RID` to prevent race-condition duplicates.
4. Provide audit-only aggregate reconciliation reporting.

## Scripts

| Script | Role |
|--------|------|
| `preflight_duplicate_review_rids.py` | Report duplicate RID groups; exit non-zero on conflicts |
| `preflight_numeric_ratings.py` | Report malformed ratings that cannot convert safely |
| `apply_migration.py` | Run both preflights, then ALTER + CREATE UNIQUE INDEX |
| `audit_aggregates.py` | Report stored vs calculated aggregates; **no writes** |

## Apply order

```bash
cd pythonCoding/bidApp
export DATABASE_URL='mysql+pymysql://...'
python migrations/pr19_reviews_ratings_decimal_unique_rid/preflight_duplicate_review_rids.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/preflight_numeric_ratings.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/apply_migration.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/audit_aggregates.py
```

## Rules

* Do **not** silently delete or merge duplicate historical rows.
* Do **not** coerce malformed rating values.
* Migration apply **stops** if preflight conflicts exist.
* Aggregate repair is **out of scope** — review `audit_aggregates.py` output and obtain separate approval.

## Rollback notes

* Rolling back unique indexes re-opens race-condition duplicate risk after Flutter depends on `409 ALREADY_REVIEWED` + DB uniqueness. Prefer keeping indexes.
* Rolling back `DECIMAL(2,1)` to integer columns **destroys half-star precision** already written by PR19 clients. Do not casually revert precision.
* Prefer application rollback (Flutter → PHP) over DB precision/uniqueness rollback unless a documented incident requires it.
* PHP handlers remain present regardless of this migration.
