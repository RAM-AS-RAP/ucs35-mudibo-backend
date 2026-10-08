# AgriSence Backend API

FastAPI backend for a SHAP-explainable crop-price decision support and farmer-buyer marketplace platform for smallholder farmers in Kenya. Forecasts and observed prices are read from PostgreSQL; the monthly ML pipeline is maintained separately.

- ML repository: https://github.com/RAM-AS-RAP/ucs35-mudibo-ml
- React PWA repository: https://github.com/RAM-AS-RAP/ucs35-mudibo-frontend

## Prerequisites

- Python 3.12
- Git
- Docker Desktop (only for a local Supabase database)
- Supabase CLI (only for local Supabase or applying remote migrations)

The API itself runs on Windows, macOS, and Linux. For local database setup, Supabase CLI runs its services in Docker. A hosted Supabase project can be used instead.

## 1. Get the code

Clone the repository, then change to its root directory, the folder containing `app/`, `supabase/`, and `requirements.txt`.

## 2. Create a virtual environment

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, allow it for this window only, then activate:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

macOS or Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Install the API dependencies in the activated environment on any platform:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 3. Configure the environment

Create a private `.env` from the example.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS or Linux:

```bash
cp .env.example .env
```

Generate a JWT signing secret from the activated Python environment:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the generated value into `JWT_SECRET` in `.env`. It must be at least 32 characters. Keep `.env` private; it is ignored by Git. The sandbox-only `MPESA_ENV` and `MPESA_BASE_URL` values in the example must remain unchanged.

## 4. Set up PostgreSQL

Choose one database option. Auth, market, forecast, order, payment, and database-health endpoints require a migrated database. The basic health check and Swagger UI can start without opening a database connection.

### Local Supabase (recommended for repeatable development)

Install the Supabase CLI if needed. For example, with Scoop on Windows:

```powershell
scoop bucket add supabase https://github.com/supabase/scoop-bucket.git
scoop install supabase
```

With Homebrew on macOS or Linux:

```bash
brew install supabase/tap/supabase
```

Docker Desktop must be running. This repository already contains `supabase/config.toml` and the migrations; do not run `supabase init` again. From the repository root, start the local stack and apply the migrations:

```bash
supabase start
supabase db reset
```

`supabase db reset` rebuilds the **local** database and applies migrations. It clears local Supabase data. After it completes, set this local connection URL in `.env`:

```dotenv
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
```

The market reference rows are inserted by a migration. `supabase/seed.sql` is intentionally comment-only.

### Hosted Supabase project

In the Supabase dashboard, open **Settings > Database > Connection string**, select **Transaction pooler**, and copy that project-specific URI into `DATABASE_URL`. Preserve its supplied username, host, port, and database; URI-encode reserved characters in the password. Do not use the local URL above for a hosted project.

From the repository root after cloning, apply the pending migrations to a hosted project with these commands:

```bash
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase db push
```

Get `YOUR_PROJECT_REF` from the Supabase project URL or dashboard. `supabase login` authenticates the CLI account; `supabase link` associates this local checkout with the selected remote project and may prompt for its database password. Enter the password at the prompt rather than putting it in the command. Before confirming `db push`, check that the linked project is the intended target: this command changes the remote schema by applying pending files under `supabase/migrations/`. It does not run `seed.sql`.

## 5. Create the initial administrator (optional)

After the database migrations have been applied, set `SEED_ADMIN_NAME`, `SEED_ADMIN_PHONE`, and `SEED_ADMIN_EMAIL` in `.env`. The phone must be a Kenyan mobile number. Either set `SEED_ADMIN_PASSWORD` to a password of at least 12 characters or leave it blank to be prompted twice without echoing it. Then run once per environment:

```bash
python -m scripts.seed_admin
```

The script exits without changes if a super admin already exists. Remove `SEED_ADMIN_PASSWORD` from `.env` after a successful seed. Do not run the price seed script until its FEWS file and product mappings have been configured in `scripts/seed_prices.py`.

## 6. Run the API

With the virtual environment active, from the repository root, use the same command on Windows, macOS, and Linux:

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Keep this terminal open while using the API. Stop the development server with `Ctrl+C`.

The unauthenticated process health check is `http://127.0.0.1:8000/api/v1/health`. The database health check, `http://127.0.0.1:8000/api/v1/health/db`, returns `503` with `{"status":"degraded"}` if the database is unavailable.

## 7. Open Swagger

With the API running and `APP_ENV=development`, open **http://127.0.0.1:8000/docs**. ReDoc is at **http://127.0.0.1:8000/redoc**. Interactive docs are disabled when `APP_ENV=production`.

To call protected endpoints, use `POST /api/v1/auth/login` with an existing account. Click **Authorize** in Swagger and enter the returned access token. The `/auth/register/farmer` and `/auth/register/buyer` routes can create farmer and buyer accounts when the database is available. An admin must be created with the seed step above.

## Tests and code checks

With the virtual environment active:

```bash
python -m pytest -q
ruff check app scripts tests
ruff format --check app scripts tests
```

Database tests are marked `db`, skipped by default, and refuse to run when `APP_ENV=production`. The normal test suite uses fake sessions and does not connect to Supabase.

## Implemented scope

Authentication, health checks, market/price/forecast reads, order operations, and payment callback handling have service implementations. Listing and admin service operations remain placeholders; those routes are visible in Swagger but are not yet operational. Forecast and price data must be populated by the separate ML pipeline or the configured FEWS import before they return useful results.

## Production start

Run behind a trusted reverse proxy that sets `X-Forwarded-For`. Restrict `--forwarded-allow-ips` to trusted proxy addresses in deployments where they are known; `*` trusts all sources and should only be used when the app is not directly reachable by untrusted clients.

```bash
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="*"
```

## Environment reference

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | Runtime environment; set to `development` for Swagger. |
| `DATABASE_URL` | Local Postgres URL or Supabase transaction-pooler URI. |
| `SUPABASE_URL` | Supabase project URL used for Storage only. |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only Storage credential. Never expose it to a client. |
| `SUPABASE_STORAGE_BUCKET` | Listing image bucket. |
| `JWT_SECRET` | Token signing secret; minimum 32 characters. |
| `JWT_ALGORITHM` | Access-token signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime, from 5 to 1440 minutes. |
| `CORS_ORIGINS` | Comma-separated allowed frontend origins. |
| `LOGIN_MAX_FAILURES` | Failed identifier attempts allowed per window. |
| `LOGIN_FAILURE_WINDOW_MINUTES` | Per-identifier throttle window. |
| `MPESA_ENV` | Must remain `sandbox`. |
| `MPESA_BASE_URL` | Must remain `https://sandbox.safaricom.co.ke`. |
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` | Daraja sandbox OAuth credentials. |
| `MPESA_SHORTCODE` / `MPESA_PASSKEY` | Daraja sandbox STK credentials. |
| `MPESA_CALLBACK_BASE_URL` / `MPESA_CALLBACK_TOKEN` | Public callback base URL and secret path token. |
| `MPESA_STK_TARGET` | `registered` or `test_msisdn` for sandbox prompts. |
| `MPESA_TEST_MSISDN` | Sandbox test phone used by `test_msisdn` mode. |
| `ORDER_EXPIRY_MINUTES` | Pending order lifetime. |
| `RECONCILE_AFTER_SECONDS` | Age after which order reads query Daraja. |
| `MAX_IMAGE_BYTES` | Maximum listing image upload size. |
| `SEED_ADMIN_NAME`, `SEED_ADMIN_PHONE`, `SEED_ADMIN_EMAIL` | Initial super-admin profile. |
| `SEED_ADMIN_PASSWORD` | Optional initial password, at least 12 characters; remove after seeding. |

## Payments and credentials

Payments are sandbox-only. Configuration rejects any non-sandbox Daraja environment or URL; no real money is transferred. Never commit `.env`, passwords, access tokens, API keys, passkeys, database URLs, or service-role credentials.
