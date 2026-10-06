# Supabase database

SQL migrations and `seed.sql` are added in the next setup task. Migrations are the source of truth for the schema. The API connects directly to PostgreSQL with async SQLAlchemy; it does not use supabase-py for table access.