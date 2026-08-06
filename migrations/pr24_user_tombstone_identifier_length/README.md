# PR24 — Expand `usertable.userAppId` for soft tombstones

## Why

Soft tombstone identifiers use the canonical FastAPI format:

* `{originalUserAppId}.DELETED`
* `{originalUserAppId}.DELETED1`
* …

A 10-digit phone becomes at least **18** characters. The historical SQLAlchemy
model declared `String(10)`, which cannot store tombstones without truncation.

## Target

`VARCHAR(64)` on `usertable.userAppId`.

## Steps

1. Set `DATABASE_URL` (or `SQLALCHEMY_DATABASE_URL`).
2. Run preflight (read-only):

```bash
python migrations/pr24_user_tombstone_identifier_length/preflight_userappid_length.py
```

3. Apply (idempotent; runs preflight first):

```bash
python migrations/pr24_user_tombstone_identifier_length/apply_migration.py
```

## Notes

* Production preflight/apply on Azure `bidapp` completed **2026-08-04** — see
  `migrations/AZURE_DB_MIGRATION_APPLY_REPORT_2026-08-04.md`. Unit tests do not
  apply this migration to production.
* Do not silently truncate tombstones.
* Related historical tables (`requesttable.customerAppId`, etc.) are **not**
  widened in PR24; they retain original phone strings after tombstone rename.
