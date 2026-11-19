# Admin Console — `admin_users`, `admin_audit_log`

Tables for the OpenBid Admin Console API (`app_v1/admin`, routes under `/admin/v1`).
Source and console: https://github.com/ashishgtksmit/openbid-admin

## Apply

```bash
cd pythonCoding/bidApp
python migrations/admin_console/apply_migration.py          # dry run
python migrations/admin_console/apply_migration.py --apply  # execute
```

Requires `DATABASE_URL` (or `DB_*`) like other PR migrations.

## App Settings (Openbid-API)

- `ADMIN_JWT_SECRET`: 48+ random characters. The API refuses admin sign-in if it's missing or shorter than 32.
- `ADMIN_CONSOLE_ORIGINS`: comma-separated console URL(s), used for CORS.

## First admin

```bash
python -m app_v1.admin.create_admin --email you@wizzride.com --name "Your Name"
```

## Rollback

The API without `app_v1/admin` ignores these tables. To drop them:

```sql
DROP TABLE IF EXISTS admin_audit_log;
DROP TABLE IF EXISTS admin_users;
```
