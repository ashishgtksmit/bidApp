# Azure MySQL Migration Apply Report — PR39 Domain Event Outbox

**Date:** 2026-08-06  
**Operator:** Cursor agent (local machine → Azure Flexible Server)  
**Target:** `openbid-mysql.mysql.database.azure.com` / database `bidapp`  
**Server version:** MySQL 8.0.44-azure  
**Resource group:** `OpenBid`  
**Package:** `migrations/pr39_domain_event_outbox/`

---

## Summary

| Package | Result | Notes |
|---------|--------|-------|
| PR39 — `openbid_domain_outbox` | **Applied** | Table + indexes created; audit OK |

---

## Access / operational notes

1. Operator public IP `14.97.242.250` was already present on the Flexible Server firewall allow-list (no temporary rule added).
2. SSL required (`require_secure_transport`); used DigiCert CA + `DATABASE_URL` with `ssl_ca=…`.
3. Credentials from `app_v1/.env` (`DB_*`); no secrets recorded in this report.
4. Temporary `/tmp/openbid_migration_db_url.env` deleted after apply.
5. Preflight JSON probe was fixed to avoid SQLAlchemy mis-parsing `:1` inside a JSON literal (`JSON_VALID(:payload)`).

---

## Before → After

**Before:** table `openbid_domain_outbox` absent.

**After:** table created with:

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `BIGINT AI PK` | NO | |
| `eventId` | `CHAR(36)` | NO | UNIQUE `uq_openbid_domain_outbox_event_id` |
| `eventType` | `VARCHAR(64)` | NO | |
| `aggregateType` | `VARCHAR(32)` | NO | |
| `aggregateId` | `VARCHAR(64)` | NO | |
| `payload` | `JSON` | NO | identifiers only |
| `schemaVersion` | `INT` | NO | |
| `occurredAt` / `createdAt` | `DATETIME(6)` | NO | UTC event infrastructure |
| `publishedAt` | `DATETIME(6)` | YES | |
| `attemptCount` | `INT` | NO | default 0 |
| `nextAttemptAt` | `DATETIME(6)` | NO | |
| `lastErrorCode` | `VARCHAR(64)` | YES | |
| `lockedAt` / `lockedBy` | claim lease | YES | |
| `status` | `VARCHAR(16)` | NO | `pending` / `published` / `dead` |
| `actorAuthSubjectHash` | `VARCHAR(64)` | YES | optional hashed actor |

Indexes:

- `uq_openbid_domain_outbox_event_id (eventId)`
- `ix_outbox_status_next_attempt_id (status, nextAttemptAt, id)`
- `ix_outbox_locked_at_status (lockedAt, status)`
- `ix_outbox_event_type_created (eventType, createdAt)`

---

## Audit (post-apply)

```
pending=0 published=0 dead=0
oldest_pending_age_seconds=None
OK: no duplicate eventIds
OK: statuses within allowed set
OK: required fields non-null
bid_created_sample_count=0
Audit OK.
```

---

## Commands executed

```bash
python migrations/pr39_domain_event_outbox/preflight_domain_event_outbox.py
python migrations/pr39_domain_event_outbox/apply_migration.py
python migrations/pr39_domain_event_outbox/audit_domain_event_outbox.py
```

---

## Non-claims (at migration time)

- This migration report does **not** claim feature-flag enablement, canary bids, load tests, or device QA by itself.
- Subsequent rollout (2026-08-06): flags enabled on `Openbid-API`; worker functions running; Redis stream/group confirmed. Two-account mutation→WSS latency measurement still **not** claimed here.
- No Flutter / WSS / PHP / Firebase changes.
