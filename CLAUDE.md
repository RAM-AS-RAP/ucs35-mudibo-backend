# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AgriSence backend: a FastAPI + async SQLAlchemy (asyncpg) API on Supabase Postgres. It serves crop-price data and forecasts with SHAP explanations to farmers in Kenya, and runs a farmer-to-buyer produce marketplace with M-Pesa (Daraja **sandbox only**) payments. A separate ML repo (`ucs35-mudibo-ml`) writes `market_prices` and `forecasts`. This API only reads them and never loads a model or computes predictions. Python 3.12.

## Commands

Run these from the repo root with `.venv` active (on Windows: `.\.venv\Scripts\Activate.ps1`):

```bash
python -m pip install -r requirements-dev.txt          # runtime + pytest/ruff
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000   # Swagger at /docs when APP_ENV != production
python -m pytest -q                                    # unit/API tests, no DB or network
python -m pytest tests/test_listings.py::test_name -q  # single test
ruff check app scripts tests
ruff format --check app scripts tests
python -m scripts.seed_admin                           # one-time super-admin seed (reads SEED_ADMIN_* from .env)
```

DB-backed tests are marked `@pytest.mark.db` and skipped unless `RUN_DB_TESTS=1`. In PowerShell, run `$env:RUN_DB_TESTS = "1"; python -m pytest -q -m db`. Point them only at a **local** migrated database (`supabase start && supabase db reset`, then `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres`). Each test runs in a rolled-back savepoint session (`db_session` fixture), and the suite refuses to run when `APP_ENV=production`.

Ruff settings: line length 120, rules `E,F,I,UP`. Pytest uses `asyncio_mode = "auto"`.

## Architecture

**Request flow:** `app/routers/*` → `app/services/*` → `app/models/*` (ORM), with Pydantic I/O in `app/schemas/*`. Routers stay thin. They declare auth dependencies, take `AsyncSession = Depends(get_db)`, and call a service function. Business rules, row locking, and commits happen in services. All routers are mounted under `/api/v1` in `app/main.py` (`create_app()` is the factory that tests use).

**Schema ownership:** the SQL files in `supabase/migrations/` are the source of truth. The models in `app/models/` only *mirror* them and reference existing Postgres enum types without creating them. There is no Alembic. A schema change means adding a new timestamped migration and then updating the model to match. `docs/schema.md` describes the tables, triggers, views, and pg_cron jobs. Many invariants are enforced by the database itself through triggers and CHECK constraints: payment state transitions, frozen order snapshots, immutable user roles, an append-only `payment_events` table, and listing status vs. quantity consistency. Expect the DB to reject writes that break them.

**DB access:** the API connects as the table **owner**, so RLS does not apply to it. Every access rule (ownership, role) must be enforced in service code. The limited `pipeline_writer` role exists only for the ML repo. `app/db.py` creates the engine lazily on first DB use, which lets `/api/v1/health` and `/docs` work without a database. It rewrites `postgresql://` URLs to `postgresql+asyncpg://` and sets `statement_cache_size=0` for the Supabase transaction pooler. Views (`latest_forecasts`, `forecast_accuracy`) are queried with raw SQL `text()` and are not mapped as ORM tables.

**Transactions:** by the time a service runs, the request session already has a transaction open, because the auth dependency queried the user through it. Calling `session.begin()` there would raise. Services do their work and finish with a single `session.commit()`. See the comment in `order_service.create_order`. Order expiry and stock restoration run in Postgres (the `expire_stale_orders()` pg_cron job, every minute). Daraja reconciliation of stale payments happens in the backend, on demand when an order is read (`RECONCILE_AFTER_SECONDS`).

**Auth (`app/core/dependencies.py`):** JWTs are stateless, but `get_current_user_any` reloads the user from the DB on every request, so the role always comes from the DB row and never from the token or the client. `get_current_user` also blocks accounts with `must_change_password`. Use `require_role(UserRole.X, ...)`, `require_admin`, or `require_super_admin` for authorization. Login throttling uses slowapi in memory (`routers/auth.limiter`) and resets when the process restarts.

**Middleware and logging (`app/core/middleware.py`, `app/core/logging_config.py`):** pure ASGI middleware adds the request id (`X-Request-ID`), `Cache-Control: no-store` and security headers to every response, writes one `app.access` log line per request, enforces body limits, and turns unhandled exceptions into the generic 500. A redaction filter on every log handler masks secrets, codes, bearer values and phone numbers. Every route must also be listed in `tests/route_matrix.py` and have one `### METHOD /api/v1/path` heading in `docs/api.md`; after changing routes or schemas, regenerate `docs/openapi.json` with `python scripts/export_openapi.py`.

**Errors (`app/core/errors.py`):** every error response uses the envelope `{"error": {"code", "message", "request_id"[, "reason"]}}`. Database connection failures map to 503 `service_unavailable`, lock timeouts/deadlocks/serialization failures to 503 `busy`. Tests that compare whole error bodies use `tests/response_helpers.without_request_id`. Raise `AppError(status, code, message, reason=None)` or a `ServiceError` subclass, or raise `HTTPException` with `detail={"code": ..., "message": ...}`. Validation errors add `fields`/`details`. `NotImplementedError` maps to 501 for any future placeholder endpoint.

**Integrations:** `app/integrations/daraja.py` is the M-Pesa client (`redact()` for logging). Config validation rejects any non-sandbox `MPESA_ENV`/`MPESA_BASE_URL`, so keep them on sandbox. `app/integrations/storage.py` handles Supabase Storage listing images, using the service-role key server-side. `app/utils/images.py` re-encodes uploaded images as JPEG and strips their metadata.

**Settings:** `app/core/config.py` defines a `pydantic-settings` `Settings` class that reads `.env` and is cached by `get_settings()`. `JWT_SECRET` must be at least 32 characters, or settings fail to load. `tests/conftest.py` sets a test secret when none is present.

## Tests

The normal suite never touches a real DB. Tests build the app with `create_app()`, drive it through `httpx.AsyncClient` + `ASGITransport` (the `api_client` fixture), and use fake sessions and dependency overrides. An autouse fixture resets the login rate limiter between tests.

## Domain rules worth knowing

These are from `docs/decisions.md` and `Tasks/*.md`. The task specs are detailed per-module requirements and are worth reading before you change a module.
- Buyers cannot see prices or forecasts. Farmers and admins can.
- Only markets with price data (currently Nairobi and Kisumu) are offered, even though `market_locations` has four rows.
- Forecasts with stale origins are hidden.
- A missing listing and a listing owned by another farmer return the same 404.
- Login failures return one generic 401 whether the cause is an unknown user, a wrong password, or a disabled account.
- Money is computed with `Decimal` and serialized as JSON numbers rounded to 2 dp. Order totals round up to a whole shilling.
- `quantity_available` is stock after reservations. Creating an order locks the listing row (`FOR UPDATE`) and decrements its stock.
- Listing units are `kg` only.
- Farmer order history shows only the buyer's name, never their contact details.
- Registration and password changes require 8–128 characters with upper, lower, digit, and symbol. Login does not re-check the policy.

`docs/api.md` is the endpoint reference. Keep it, `docs/schema.md`, and `docs/decisions.md` in sync when behavior changes.

The root `main.py` is a stray stub and is not the entry point. The app lives at `app.main:app`.
