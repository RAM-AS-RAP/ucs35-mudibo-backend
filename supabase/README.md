# Supabase database

The SQL migrations are the source of truth for the schema. They are applied in this order:

1. `20261008000100_extensions_enums_and_helpers.sql`
2. `20261008000200_users.sql`
3. `20261008000300_market_tables.sql`
4. `20261008000400_produce_listings.sql`
5. `20261008000500_orders.sql`
6. `20261008000600_payments.sql`
7. `20261008000700_views.sql`
8. `20261008000800_security_rls_and_storage.sql`
9. `20261008000900_jobs.sql`

`supabase db reset` rebuilds the local database from migrations and then runs configured seed files. `supabase db push` applies pending migrations to the linked remote project. `db push` does not run `seed.sql`.

Market reference rows are inserted by migration 3 so they are present in local and remote databases. `seed.sql` is intentionally a comment-only placeholder. Create the initial administrator with `scripts/seed_admin.py` and load FEWS prices with `scripts/seed_prices.py` as separate manual steps.

The API connects directly to PostgreSQL with async SQLAlchemy as the database owner and bypasses RLS. It does not use supabase-py for table access. Supabase Storage is accessed over HTTP by the backend.