# Database schema

The SQL migrations in `supabase/migrations/` own the database schema. The SQLAlchemy mappings under `app/models/` mirror those migrations and reference the PostgreSQL enum types without creating them. The migration definitions were checked against the current model columns and types; no model mismatch was found.

## Enums

`user_role` (`farmer`, `buyer`, `admin`), `crop_type` (`maize`, `beans`), `forecast_horizon` (`one_month`, `two_month`), `price_source` (`fews`, `wfp`), `listing_status` (`active`, `sold_out`, `removed`), `order_status` (`pending_payment`, `paid`, `payment_failed`, `expired`, `cancelled`, `needs_reconciliation`), and `payment_status` (`initiating`, `initiated`, `completed`, `failed`, `cancelled`, `timeout`).

## Tables

- `users`: role-bound accounts, unique normalized phone numbers, case-insensitive unique email, one-super-admin constraint, disable audit fields, and immutable roles.
- `market_locations`: the four FEWS market references, inserted by migration 3.
- `market_prices`: monthly market/crop observations with positive numeric prices and first-of-month dates.
- `forecasts`: monthly-origin forecasts keyed by market/crop/horizon/origin, with horizon-checked target months and JSONB SHAP values.
- `produce_listings`: farmer-owned inventory with composite farmer-role foreign key, quantity/status invariants, and soft-removal fields.
- `orders`: buyer orders with fixed listing, buyer, quantity, price, total, and reference snapshots.
- `payments`: idempotent Daraja attempts with amount/order validation, one-open-attempt-per-order, and guarded status transitions.
- `payment_events`: JSONB event log that rejects deletion and mutation except linking an orphan callback to its payment.

## Views

- `latest_forecasts`: one row per market/crop/horizon at the greatest `origin_month`; backfills are not filtered out.
- `forecast_accuracy`: left-joins each forecast to its realised market price and exposes actual log change, absolute and percentage error, direction correctness, persistence error, persistence comparison, and pending state. Metrics are null until an actual price exists.
- `dashboard_stats`: grouped user, disabled-user, listing, order, and payment counts plus completed sandbox payment amount.

All views use `security_invoker = on`. The API reads `latest_forecasts` and `forecast_accuracy` with SQL text rather than mapping views as ORM tables.

## Triggers and indexes

`users_guard_update`, `users_set_updated_at`, and `users_prevent_delete` enforce user-role immutability, super-admin protection, timestamps, and disable-instead-of-delete. `produce_listings_owner_immutable` and `produce_listings_set_updated_at` protect listing ownership and timestamps. `orders_guard_update` and `orders_set_updated_at` freeze order snapshots, protect paid orders, and maintain timestamps. `payments_validate_insert`, `payments_guard_update`, and `payments_set_updated_at` validate pending order/amount, enforce the allowed payment state graph, make request identity fields immutable, allow a checkout ID to be assigned once, and maintain timestamps. `payment_events_append_only` permits only orphan-callback payment linking as an update. `set_updated_at()` is the shared timestamp trigger function; `direction_class(numeric)` classifies forecast direction around the +/-0.01 threshold.

Named indexes are `users_email_lower_unique_idx`, `users_one_super_admin_idx`, `market_prices_month_idx`, `forecasts_target_month_idx`, `forecasts_series_latest_idx`, `produce_listings_farmer_id_idx`, `produce_listings_market_crop_status_idx`, `orders_buyer_created_idx`, `orders_listing_status_idx`, `orders_pending_expiry_idx`, `payments_receipt_unique_idx`, `payments_one_open_per_order_idx`, `payments_order_id_idx`, `payments_status_created_idx`, `payment_events_payment_created_idx`, and `payment_events_checkout_request_idx`. Primary keys and unique constraints additionally index user IDs, phone numbers, `(id, role)`, FEWS IDs, composite price/forecast keys, listing IDs, order IDs/references, payment IDs/idempotency keys/checkout IDs, and payment-event IDs.

## Access and jobs

RLS is enabled on all eight tables with no policies and without `FORCE ROW LEVEL SECURITY`. Data API roles `anon` and `authenticated` have no table, sequence, or function privileges. The backend connects as the table owner. The public `listing-images` Storage bucket accepts JPEG and PNG files up to 5 MiB; uploads are performed by the backend service role.

The `expire-stale-orders` pg_cron job runs every minute. `expire_stale_orders()` expires pending orders, times out their open payment attempts, restores reserved stock, and reactivates sold-out listings. Daraja STK Push Query reconciliation remains in the backend because PostgreSQL cannot call Daraja.

The separate ML repository's monthly pipeline writes `market_prices` and `forecasts`. This API reads those tables/views and never loads a model or computes predictions.