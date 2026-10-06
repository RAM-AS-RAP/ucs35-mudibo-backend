# Database schema

The SQL migrations in `supabase/migrations/` own the database schema. The SQLAlchemy mappings under `app/models/` mirror those migrations and reference the PostgreSQL enum types without creating them.

Tables: `users`, `market_locations`, `market_prices`, `forecasts`, `produce_listings`, `orders`, `payments`, and `payment_events`.

Views: `latest_forecasts` selects the latest origin month for each market, crop, and horizon. `forecast_accuracy` joins forecasts to realised prices and exposes error and comparison metrics. Views are queried with SQL text and are not mapped as tables.

The separate ML repository's monthly pipeline writes `market_prices` and `forecasts`. This API reads those tables/views and never loads a model or computes predictions.