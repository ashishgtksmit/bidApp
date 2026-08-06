# PR37 — Account session identity (`sessionVersion` + `accountSessionId`)

**Status:** Migration package implemented. Production apply on Azure `bidapp`
(**2026-08-04**) — see `migrations/AZURE_DB_MIGRATION_APPLY_REPORT_2026-08-04.md`.

## Purpose

Add per-account session identity fields used by JWT access/refresh validation:

| Column | Type | Rules |
|--------|------|-------|
| `sessionVersion` | `INT NOT NULL` | Starts at `1`; incremented on password reset and account deletion |
| `accountSessionId` | `VARCHAR(64) NOT NULL UNIQUE` | Cryptographically random UUID/hex; one per DB account row; retained on tombstones |

These claims appear in JWTs as `session_version` and `session_id`. They are
**never** exposed in Flutter/API profile responses.

## Why

- Prevent old refresh tokens from authenticating a recreated account that
  reuses the same phone (`userAppId`).
- Invalidate all tokens after password reset / account deletion via
  `sessionVersion` bump.
- Keep JWT `sub = userAppId/phone` for PR37 (UID-sub migration is a future PR).

## Files

| File | Role |
|------|------|
| `preflight_account_session_identity.py` | Read-only safety checks |
| `apply_migration.py` | Staged add → backfill → NOT NULL → unique index |
| `audit_account_session_identity.py` | Post-apply invariants |
| `README.md` | This document |

## Apply order (backend-first)

1. Run preflight.
2. Run apply (this migration).
3. Deploy FastAPI JWT/refresh/login changes.
4. Verify new tokens include `session_version` / `session_id` / `jti`.
5. Deploy Flutter (stop sending Bearer access on `/refresh`).

Older Flutter builds may still send `Authorization`; FastAPI must ignore it for
refresh ownership.

## Commands

From `pythonCoding/bidApp` with `DATABASE_URL` (or `SQLALCHEMY_DATABASE_URL`) set:

```bash
python migrations/pr37_account_session_identity/preflight_account_session_identity.py
python migrations/pr37_account_session_identity/apply_migration.py
python migrations/pr37_account_session_identity/audit_account_session_identity.py
```

## Behaviour

1. Add nullable `sessionVersion` / `accountSessionId` if missing.
2. Backfill `sessionVersion = 1`.
3. Backfill unique secure-random `accountSessionId` per row.
4. Verify no nulls / empties / duplicates.
5. Make columns `NOT NULL`.
6. Add unique index on `accountSessionId`.
7. Preserve `UID`, `userAppId`, tombstone identifiers, profile/KYC/business data.

## Notes

- Idempotent skip when columns/index already present.
- Does **not** create refresh-token family tables.
- Does **not** migrate JWT subject to UID.
- Forced re-login: legacy refresh tokens without both session claims are rejected
  after FastAPI deploy.
