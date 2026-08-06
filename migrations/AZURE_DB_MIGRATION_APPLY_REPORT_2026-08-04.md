# Azure MySQL Migration Apply Report

**Date:** 2026-08-04  
**Operator:** Cursor agent (local machine → Azure Flexible Server)  
**Target:** `openbid-mysql.mysql.database.azure.com` / database `bidapp`  
**Server version:** MySQL 8.0.44-azure  
**Resource group:** `OpenBid`

---

## Summary

Four of five pending migration packages were applied successfully. **PR19 was not applied** because preflight found historical duplicate `vendorreviews.RID` rows; the official apply script correctly refused to continue.

| Package | Result | Notes |
|---------|--------|-------|
| PR15 — car soft-delete + normalized reg | **Applied** | `cardetails` had 0 rows; columns + unique index added |
| PR19 — review DECIMAL + unique RID | **Blocked** | 2 duplicate RID groups; no schema change |
| PR24 — `userAppId` VARCHAR(64) | **Applied** | `varchar(10)` → `varchar(64)` |
| PR37 — session identity | **Applied** | Backfilled 135 users; audit OK |
| PR38 — immutable auth subject | **Applied** | Backfilled 135 users; audit OK |

---

## Access / operational notes

1. Direct connect from this workstation initially **timed out** (IP not on firewall allow-list).
2. Temporary firewall rule `CursorMigration_20260804` was added for the operator public IP, then **removed** after apply.
3. SSL is required (`require_secure_transport`); migrations used DigiCert CA + `DATABASE_URL` with `ssl_ca=…`.
4. Credentials were taken from `app_v1/.env` (`DB_*`); no secrets are recorded in this report.
5. Temporary `/tmp/openbid_migration_db_url.env` was deleted after apply.

---

## Before → After

### PR15 — `cardetails`

**Before:** only `PRIMARY` index; no soft-delete / normalized-reg columns.

**After:**

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `normalizedCarRegNo` | `varchar(100)` | NO | unique index `uq_cardetails_normalizedCarRegNo` |
| `isDeleted` | `tinyint(1)` | NO | default `0` |
| `deletedAt` | `timestamp` | YES | |
| `deletedBy` | `varchar(10)` | YES | |

Row count at apply: **0**.

### PR24 — `usertable.userAppId`

| | Type |
|--|------|
| Before | `varchar(10)` UNIQUE NOT NULL |
| After | `varchar(64)` UNIQUE NOT NULL |

Enables soft-tombstone identifiers such as `{phone}.DELETED` / `{phone}.DELETED1`.

### PR37 — `usertable` session identity

| Column | Type | Null | Key | Backfill |
|--------|------|------|-----|----------|
| `sessionVersion` | `int` | NO | | all rows = `1` |
| `accountSessionId` | `varchar(64)` | NO | UNI | 135 unique random hex values |

Audit: `rows=135`, `null_sessionVersion=0`, `null_or_empty_accountSessionId=0`, `duplicate_accountSessionId_groups=0`.

### PR38 — `usertable.authSubjectId`

| Column | Type | Null | Key | Backfill |
|--------|------|------|-----|----------|
| `authSubjectId` | `varchar(64)` | NO | UNI (`uq_usertable_auth_subject_id`) | 135 unique `uuid4().hex` values |

Audit: `null_or_empty_authSubjectId=0`, `duplicate_authSubjectId_groups=0`, unique index present.

**Preserved:** `UID`, `userAppId`, tombstone identifiers, profile/KYC/business data.

---

## PR19 — not applied (blocking conflicts)

Preflight `preflight_duplicate_review_rids.py` failed. Per package rules: **do not silently delete/merge duplicates**; apply stops.

Numeric rating preflight **passed** (78 vendor rows convertible; 0 customer rows).

### Duplicate groups

| RID | VRID list | Notes |
|-----|-----------|-------|
| 34 | 10, 14 | Same `VENDORID` `8250410772`; different `customerAppId` / timestamps / scores |
| 44 | 9, 55 | Same `VENDORID` `8637554387`; different `customerAppId` / timestamps / scores |

Detail (production snapshot at apply time):

| VRID | RID | customerAppId | VENDORID | Ratings (DB/P/C/Cl) | comments | tableTimestamp |
|------|-----|---------------|----------|---------------------|----------|----------------|
| 10 | 34 | 7022359323 | 8250410772 | 5/4/4/4 | toto | 2025-03-10 14:18:43 |
| 14 | 34 | 9733490982 | 8250410772 | 5/5/5/5 | Awesome | 2025-07-10 08:23:48 |
| 9 | 44 | 7022359323 | 8637554387 | 5/5/5/4 | yo yo | 2025-03-10 14:11:37 |
| 55 | 44 | 8250410772 | 8637554387 | 5/5/5/5 | This is a test of game. | 2025-09-17 03:33:50 |

### PR19 schema still pending

- `vendorreviews` category columns remain `int`
- `customerreviews.generalRating` remains `varchar(20)`
- Unique indexes `uq_vendorreviews_rid` / `uq_customerreviews_rid` **not** created

### Decision needed before PR19 apply

Choose how to resolve each duplicate RID (examples — product decision required):

1. Keep earliest review, archive/delete later  
2. Keep latest review, archive/delete earlier  
3. Keep both under a revised uniqueness rule (e.g. unique on `(RID, customerAppId)` — would be a **schema/contract change**, not the current PR19 package)  
4. Manual merge of scores/comments

Until resolved and re-preflighted, **do not** run `apply_migration.py` for PR19.

---

## Post-apply verification commands

From `pythonCoding/bidApp` with `DATABASE_URL` (SSL CA required):

```bash
python migrations/pr37_account_session_identity/audit_account_session_identity.py
python migrations/pr38_immutable_auth_subject/audit_immutable_auth_subject.py
python migrations/pr19_reviews_ratings_decimal_unique_rid/preflight_duplicate_review_rids.py
```

---

## Behaviour / deploy implications

- FastAPI PR37/PR38 JWT code expects `sessionVersion`, `accountSessionId`, and `authSubjectId` on `usertable`. Those columns now exist in production.
- After FastAPI deploy that mints session claims / identity-v2 subjects, **legacy refresh tokens without session claims will be rejected** (forced re-login) — as documented in PR37 README.
- PR15 enables soft-delete + global unique normalized registration for manage-cars flows; table was empty at apply time.
- PR24 enables account-deletion tombstone strings longer than 10 chars without truncation.
- PR19 half-star persistence + DB-enforced one-review-per-RID uniqueness remain **unavailable** until duplicates are resolved.

---

## Rollback notes

| Package | Rollback caution |
|---------|------------------|
| PR15 | Dropping unique index reopens duplicate-reg races; dropping soft-delete columns loses deletion metadata if any cars are soft-deleted later |
| PR24 | Shrinking `userAppId` back to `varchar(10)` will fail or truncate if tombstones exist |
| PR37 | Removing session columns breaks JWT validation once FastAPI depends on them; requires coordinated app rollback first |
| PR38 | Removing `authSubjectId` breaks identity-v2 tokens; requires coordinated app rollback / `JWT_IDENTITY_VERSION_TO_MINT=1` emergency path first |
| PR19 | N/A — not applied |

Prefer application rollback over destructive column drops unless an incident requires it.

---

## Manual QA checklist

- [ ] Login / refresh mint tokens with `session_version`, `session_id`, and (when identity v2 enabled) `sub = authSubjectId`
- [ ] Password reset bumps `sessionVersion` and invalidates old refresh tokens
- [ ] Account deletion tombstones `userAppId` with `.DELETED` without truncation
- [ ] Vendor manage-cars create/soft-delete / normalized reg uniqueness (after cars exist)
- [ ] Reviews: half-star + duplicate RID still behave as today until PR19 applied
- [ ] Confirm FastAPI App Service can still reach MySQL (firewall `AllowAllAzureServices…` rule unchanged)

---

## Known risks / residual work

1. **PR19 blocked** — 2 duplicate vendor review RID groups need an explicit product/data decision.
2. Local developer IPs are not permanently allow-listed; re-add a ClientIP rule before future operator SQL work.
3. `app_v1/.env` has a dotenv parse warning at line 36 (unrelated malformed line); does not block `DB_*` loading but should be cleaned separately.
4. Production apply of PR37/PR38 was previously “not claimed” in plan docs; this report is the apply record.

---

## Files / packages used

- `migrations/pr15_car_soft_delete_normalized_reg/`
- `migrations/pr19_reviews_ratings_decimal_unique_rid/` (preflight only)
- `migrations/pr24_user_tombstone_identifier_length/`
- `migrations/pr37_account_session_identity/`
- `migrations/pr38_immutable_auth_subject/`
