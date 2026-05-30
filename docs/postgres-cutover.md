# Production cutover: SQLite → PostgreSQL

This runbook flips the production app on `ealedgers.com` from the bundled
SQLite file to a real PostgreSQL database, and activates the Row-Level
Security policies shipped in migration `0008_postgres_rls`.

**Do this once.** It is a manual, supervised operation — there is no
"flip a switch" deploy hook on purpose, because rolling forward to
Postgres is a one-way trip without a careful reverse plan.

Audience: someone who already has SSH into the Vultr box at
`45.32.150.96` and can `sudo` as `root`.

This file is referenced by Step 3 of `docs/EXECUTION_PLAN.md`.

---

## Prerequisites

- A `db.sqlite3.pre-pg.bak` backup of the current production DB.
- About 30 minutes of downtime budget.
- An off-host backup of `db.sqlite3` already exists (vhost or rsync target —
  anywhere not on the same disk).

---

## 1. Install Postgres on the server

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
sudo systemctl enable --now postgresql
sudo -u postgres psql -c "SHOW server_version;"
```

PostgreSQL 16+ is fine. The `set_config()` function and RLS have been
stable since 9.5.

## 2. Create the database role and database

```bash
sudo -u postgres psql <<'EOF'
CREATE ROLE ealedgers LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';
CREATE DATABASE ealedgers OWNER ealedgers ENCODING 'UTF8' TEMPLATE template0;
GRANT ALL PRIVILEGES ON DATABASE ealedgers TO ealedgers;
EOF
```

The role intentionally is **not** a superuser — Postgres RLS bypasses
the policies for superusers, which would defeat the defence-in-depth
goal.

## 3. Install psycopg + bump deps on the server

```bash
cd /var/www/ealedgers
sudo -u www-data ./venv/bin/pip install "psycopg[binary]>=3.1"
```

## 4. Dump the SQLite data

```bash
cd /var/www/ealedgers
sudo cp db.sqlite3 db.sqlite3.pre-pg.bak
sudo -u www-data ./venv/bin/python manage.py dumpdata \
    --exclude=contenttypes --exclude=auth.permission \
    --natural-foreign --natural-primary \
    --indent=2 \
    > /tmp/ealedgers-dump.json
```

Sanity check: the dump should be on the order of a few MB and contain
1,165 SYSCOHADA accounts plus the Elite Advisors tenant rows.

```bash
grep -c '"model": "accounting.account"' /tmp/ealedgers-dump.json   # ~1165
grep -c '"model": "accounting.tenant"' /tmp/ealedgers-dump.json    # ≥ 1
```

## 5. Point Django at Postgres and run migrations

Edit the systemd EnvironmentFile (or wherever `DJANGO_*` env vars are set
for gunicorn). Add:

```
DATABASE_URL=postgres://ealedgers:REPLACE_WITH_STRONG_PASSWORD@127.0.0.1:5432/ealedgers
```

Reload gunicorn config (but **don't restart yet** — we still need to
load data into the fresh schema):

```bash
sudo systemctl daemon-reload
```

Then create the schema in Postgres:

```bash
sudo systemctl stop gunicorn-ealedgers   # stop traffic
sudo -u www-data DATABASE_URL='postgres://...' \
    ./venv/bin/python manage.py migrate --noinput
```

This runs migrations `0001` through `0008` against the empty Postgres
database. Migration `0008_postgres_rls` installs the RLS policies on the
14 tenant-scoped tables.

## 6. Load the SQLite dump into Postgres

```bash
sudo -u www-data DATABASE_URL='postgres://...' \
    ./venv/bin/python manage.py loaddata /tmp/ealedgers-dump.json
```

If you hit a content-type / permission constraint error, that's because
`auth.permission` rows were re-created by `migrate`. The `--exclude` in
step 4 should have skipped those; if you forgot it, re-dump.

## 7. Sanity check

```bash
sudo -u www-data DATABASE_URL='postgres://...' \
    ./venv/bin/python manage.py shell -c "
from accounting.models import Account, Tenant, Journal, JournalEntry
print('Tenants:', Tenant.objects.count())
print('Accounts:', Account.objects.count())
print('Journals:', Journal.objects.count())
print('Entries:', JournalEntry.objects.count())
"
```

Expected: matches what's in the SQLite backup.

## 8. Start gunicorn

```bash
sudo systemctl start gunicorn-ealedgers
sudo systemctl status gunicorn-ealedgers
curl -I https://ealedgers.com/
```

Hit the live URL. Log in, navigate the workspace, open an invoice, run
a report. RLS is now active — the middleware sets
`app.current_tenant_id` per-request, and Postgres filters rows server-side.

## 9. Verify RLS actually works

Connect to Postgres directly as the `ealedgers` user (no superuser):

```bash
psql -U ealedgers -d ealedgers -h 127.0.0.1
```

```sql
-- No tenant pinned: should return 0 rows even though the table has data.
SELECT count(*) FROM accounting_account;
-- → 0

-- Pin tenant 1 (Elite Advisors).
SELECT set_config('app.current_tenant_id', '1', false);
SELECT count(*) FROM accounting_account;
-- → 1165 (or however many you had)

-- Pin a non-existent tenant.
SELECT set_config('app.current_tenant_id', '999', false);
SELECT count(*) FROM accounting_account;
-- → 0
```

If those three checks all show what's expected, RLS is doing its job.

## 10. Decommission SQLite

After 24–48 hours of healthy traffic on Postgres, archive the old DB:

```bash
sudo mv /var/www/ealedgers/db.sqlite3 /var/www/ealedgers/db.sqlite3.archived.$(date +%F)
```

Keep the `.pre-pg.bak` snapshot indefinitely.

---

## Rollback plan

If something goes wrong before step 8 (gunicorn restart):

```bash
sudo systemctl stop gunicorn-ealedgers
# Remove the DATABASE_URL env var from the systemd EnvironmentFile
sudo systemctl daemon-reload
sudo systemctl start gunicorn-ealedgers
```

This sends the app back to SQLite (unchanged on disk — we never wrote
to it during the cutover).

If something goes wrong **after** step 8 (traffic on Postgres for some
time), the rollback is more delicate because new invoices/entries have
landed in Postgres. You can either:

1. `pg_dump` Postgres, transform to a Django fixture, load back into a
   fresh SQLite — laborious, error-prone — or
2. Fix forward on Postgres.

Option 2 is almost always the right call once traffic has landed.

---

## Notes on the RLS implementation

- The `tenant_isolation` policy uses `NULLIF(current_setting('app.current_tenant_id', true), '')::bigint` so an unset
  variable doesn't raise — it returns NULL, which never matches `tenant_id`,
  so the query returns 0 rows. Safer than erroring out.
- `FORCE ROW LEVEL SECURITY` is set so the table owner (the `ealedgers`
  role) also has policies enforced. Without `FORCE`, the owner bypasses
  RLS.
- Superusers in Postgres still bypass RLS by default. Don't run the app
  as a superuser. If you need cross-tenant queries from psql, use
  `SET LOCAL row_security = off;` inside a transaction.
