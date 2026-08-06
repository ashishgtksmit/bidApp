# PR39 — Domain event transactional outbox (`openbid_domain_outbox`)

**Status:** Migration package implemented. Production apply **not** claimed.

## Purpose

Create the transactional outbox table used by the PR39 event-driven snapshot
pipeline:

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGINT AI PK` | Claim / ordering |
| `eventId` | `CHAR(36) UNIQUE` | Idempotent event identity |
| `eventType` | `VARCHAR(64)` | Registry key (`bid.created` canary) |
| `aggregateType` | `VARCHAR(32)` | e.g. `request` |
| `aggregateId` | `VARCHAR(64)` | RID string |
| `payload` | `JSON` | Identifiers only |
| `schemaVersion` | `INT` | Envelope version |
| `occurredAt` / `createdAt` | `DATETIME(6)` UTC | Event infrastructure time |
| `publishedAt` | `DATETIME(6) NULL` | Dispatch success |
| `attemptCount` / `nextAttemptAt` | retry | |
| `lastErrorCode` | `VARCHAR(64) NULL` | Safe ops code |
| `lockedAt` / `lockedBy` | claim lease | |
| `status` | `pending` / `published` / `dead` | |
| `actorAuthSubjectHash` | `VARCHAR(64) NULL` | Optional hashed actor |

No tokens, FCM, KYC, bank, chat, passwords, phones, or recipient lists.

## Files

| File | Role |
|------|------|
| `preflight_domain_event_outbox.py` | Read-only safety checks |
| `apply_migration.py` | Create table + indexes (idempotent) |
| `audit_domain_event_outbox.py` | Counts / duplicates / invalid statuses |
| `README.md` | This document |

## Commands

From `pythonCoding/bidApp` with `DATABASE_URL` (or `DB_*`) set:

```bash
python migrations/pr39_domain_event_outbox/preflight_domain_event_outbox.py
python migrations/pr39_domain_event_outbox/apply_migration.py
python migrations/pr39_domain_event_outbox/audit_domain_event_outbox.py
```

## Behaviour

1. Preflight verifies MySQL ≥ 8 (SKIP LOCKED), JSON support, privileges, and
   that an existing table is compatible (stops on incompatible schema).
2. Apply creates `openbid_domain_outbox` + required indexes; adds
   `actorAuthSubjectHash` if an older compatible table lacks it.
3. Audit reports pending/published/dead counts, oldest pending age, duplicate
   `eventId`, invalid statuses, and null/invalid required fields.

## Notes

- Idempotent where practical (`CREATE TABLE IF NOT EXISTS`, index presence checks).
- Does **not** drop or rewrite unrelated tables.
- Production apply is **not** claimed by this package alone.
- Feature flags default off: `DOMAIN_EVENTS_ENABLED=false`,
  `DOMAIN_EVENT_BID_CREATED_ENABLED=false`.
