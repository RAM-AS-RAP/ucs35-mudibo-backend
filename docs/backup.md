# Backups

The Supabase free plan has **no point-in-time recovery** and only limited automatic backups. Take a logical backup yourself **before every migration and before every demonstration**, and keep it somewhere other than your laptop's working folder (an encrypted drive or a private cloud folder). A dump contains personal data and password hashes, so treat it like a secret and never commit it.

## Logical backup with `pg_dump`

You need a machine with the PostgreSQL client tools (`pg_dump`, `pg_restore`, `psql`), version 15 or newer to match Supabase.

1. In the Supabase dashboard, open **Connect** and copy the **Session pooler** connection string (port **5432**, works over IPv4). The transaction pooler (port 6543) is not suitable for `pg_dump`, and the direct connection may be IPv6 only.
2. Put it in an environment variable, not in your shell history:

   ```bash
   read -s BACKUP_URL   # paste the session pooler URI, then press Enter
   export BACKUP_URL
   ```

   In PowerShell: `$env:BACKUP_URL = Read-Host -MaskInput`.

3. Dump the `public` schema (tables, data, views, functions, triggers, policies) in the custom format:

   ```bash
   pg_dump "$BACKUP_URL" --format=custom --no-owner --no-privileges --schema=public \
     --file="agrisence-$(date +%Y%m%d-%H%M).dump"
   ```

4. Check the file lists its tables: `pg_restore --list agrisence-<date>.dump | head`.

## Restore

Restore only into a **new or empty** project or a local database, never over the live one without a plan:

```bash
pg_restore --dbname "$RESTORE_URL" --no-owner --no-privileges --clean --if-exists agrisence-<date>.dump
```

Then re-run the role and grant parts of the migrations if needed (`pipeline_writer`, RLS, Storage policies), because `--no-owner --no-privileges` skips ownership and grants. Check the result with the queries in `supabase/verification/`. The `pg_cron` jobs live in the `cron` schema, so recreate them from `supabase/migrations/20261008000900_jobs.sql` when restoring into a new project.

## Fallback: CSV export

If no machine with `pg_dump` is available, open each table in the Supabase **Table Editor** and use **Export → CSV**. This keeps the data but not the schema, triggers or policies, which come from `supabase/migrations/`. Export at least `users`, `produce_listings`, `orders`, `payments`, `payment_events`, `market_prices`, `forecasts`, `admin_invites`.

## What a database dump does not contain

- **Storage objects**: listing images live in the `listing-images` Storage bucket, not in the database. Download them separately (dashboard, or the Storage API with the service role key) if you need them. Without them, `image_url` values point to missing files.
- Supabase project settings, API keys, auth settings and secrets. Keep `.env` values in your password manager.
- The ML pipeline's own files and models (separate repository).
