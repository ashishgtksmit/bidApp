# PR38 — Immutable auth subject (`authSubjectId`)

**Status:** Migration package implemented. Production apply on Azure `bidapp`
(**2026-08-04**) — see `migrations/AZURE_DB_MIGRATION_APPLY_REPORT_2026-08-04.md`.

## Purpose

Add a stable, immutable auth subject identifier used by JWT `sub` claims:

| Column | Type | Rules |
|--------|------|-------|
| `authSubjectId` | `VARCHAR(64) NOT NULL UNIQUE` | Cryptographically random UUID/hex (`uuid.uuid4().hex`); one per DB account row; retained on tombstones |

**Never** derive `authSubjectId` from phone, `UID`, email, or `accountSessionId`.

## Why

- Decouple JWT subject from mutable identifiers (phone / `userAppId`).
- Keep the same subject across password reset and soft-delete tombstones so
  session identity (`sessionVersion` / `accountSessionId` from PR37) remains
  the revocation signal, not subject churn.
- Support dual-read of legacy phone `sub` while minting identity v2 tokens.

## Files

| File | Role |
|------|------|
| `preflight_immutable_auth_subject.py` | Read-only safety checks |
| `apply_migration.py` | Staged add → backfill → NOT NULL → unique index |
| `audit_immutable_auth_subject.py` | Post-apply invariants |
| `README.md` | This document |

## Apply order (backend-first)

1. Run preflight.
2. Run apply (this migration).
3. Deploy FastAPI JWT/login/refresh with dual-read support.
4. Verify new tokens mint `sub = authSubjectId` with identity version 2.
5. Deploy Flutter (consume identity-v2 `sub`; keep refresh ownership rules).

Recommended FastAPI defaults during rollout:

- `JWT_ALLOW_LEGACY_PHONE_SUB=true` — accept legacy phone/`userAppId` subjects
  while clients upgrade.
- `JWT_IDENTITY_VERSION_TO_MINT=2` — mint new tokens with immutable
  `authSubjectId` subject.

## Commands

From `pythonCoding/bidApp` with `DATABASE_URL` (or `SQLALCHEMY_DATABASE_URL`) set:

```bash
python migrations/pr38_immutable_auth_subject/preflight_immutable_auth_subject.py
python migrations/pr38_immutable_auth_subject/apply_migration.py
python migrations/pr38_immutable_auth_subject/audit_immutable_auth_subject.py
```

## Behaviour

1. Add nullable `authSubjectId` if missing.
2. Backfill every row (including tombstones) with `uuid.uuid4().hex`
   (collision-safe retry; never derived from phone / UID / email /
   `accountSessionId`).
3. Verify no nulls / empties / duplicates.
4. Make column `NOT NULL`.
5. Add unique index `uq_usertable_auth_subject_id` if missing.
6. Preserve `UID`, `userAppId`, `sessionVersion`, `accountSessionId`,
   tombstone identifiers, profile/KYC/business data.

## Notes

- Idempotent skip when column/index already present.
- Does **not** remove legacy phone `sub` acceptance (controlled by
  `JWT_ALLOW_LEGACY_PHONE_SUB`).
- Does **not** change PR37 session identity columns.
- Production apply is **not** claimed by this package alone.
