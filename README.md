# ucs35-mudibo-backend
## Project Title: SHAP-based explainable decision support system for crop price prediction and farmer-buyer linkage among smallholder farmers in Kenya
### Name: Sean Mudibo
### Adm No: 168329
### Capstone Project (Webmasters Kenya)

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

Install the dependencies in the activated environment on any platform. `requirements-dev.txt` installs the pinned runtime packages from `requirements.txt` plus the test and lint tools:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

A deployment that only runs the API needs `requirements.txt` alone.

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

### Pipeline database role

The separate ML repository should connect as the limited `pipeline_writer` role, not as `postgres`. To set it up on the linked hosted project:

1. Apply the additive role migration after reviewing the target project:

	```bash
	supabase db push
	```

2. Generate a password locally with Python:

	```bash
	python -c "import secrets; print(secrets.token_urlsafe(32))"
	```

	In the Supabase SQL Editor, set the role password, replacing the placeholder with the generated value:

	```sql
	alter role pipeline_writer with password '<value>';
	```

	The generated URL-safe value can be embedded in a connection URI. Never commit it or paste it into a file in either repository. Store it only in the ML repository's GitHub secret.

3. Run `supabase/verification/pipeline_role_checks.sql` manually as `postgres` in the Supabase SQL Editor. It verifies allowed upserts and denied access inside a transaction and rolls back its test rows. If the hosted SQL Editor rejects `SET ROLE`, use the manual connection test below instead.

The pipeline role can read `market_locations`, read and insert `market_prices`/`forecasts`, and update only their approved non-key columns. It cannot access users, listings, orders, payments, payment events, or views. The backend API continues using its existing database-owner connection and never uses this role.

For the ML GitHub Actions secret `PIPELINE_DATABASE_URL`, use the transaction-pooler URI shape below. Copy the pooler host and port from the Supabase dashboard's **Connect** panel, and confirm the custom-role username format shown there. The usual custom-role form is:

```text
postgresql://pipeline_writer.<project-ref>:<password>@<pooler-host>:6543/postgres
```

You can test the role manually by substituting the dashboard values and generated password in this `psql` command, then running the queries below:

```bash
psql "postgresql://pipeline_writer.<project-ref>:<password>@<pooler-host>:6543/postgres"
```

```sql
select count(*) from market_prices; -- should succeed
select count(*) from users;         -- should fail with permission denied
```

To rotate the credential, generate a new value with the Python command above, run `alter role pipeline_writer with password '<new value>';` in the SQL Editor, and update the ML repository's `PIPELINE_DATABASE_URL` secret. Do not place the real URI or password in either repository.

### Hosted Supabase project

In the Supabase dashboard, open **Settings > Database > Connection string**, select **Transaction pooler**, and copy that project-specific URI into `DATABASE_URL`. Preserve its supplied username, host, port, and database; URI-encode reserved characters in the password. Do not use the local URL above for a hosted project.

From the repository root after cloning, apply the pending migrations to a hosted project with these commands:

```bash
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase db push
```

Get `YOUR_PROJECT_REF` from the Supabase project URL or dashboard. `supabase login` authenticates the CLI account; `supabase link` associates this local checkout with the selected remote project and may prompt for its database password. Enter the password at the prompt rather than putting it in the command. Before confirming `db push`, check that the linked project is the intended target: this command changes the remote schema by applying pending files under `supabase/migrations/`. It does not run `seed.sql`.

### Admin migration (Module 6)

The admin routes need migration `20261015000100_admin_resolution_event.sql`, which allows the `admin_action` payment event type used to record order resolutions. A fresh `supabase db reset` (local) applies it with the others. For an existing hosted project, apply it with `supabase db push` as above (same command on every OS), then run `supabase/verification/admin_event_check.sql` in the Supabase SQL Editor. It works inside a rolled-back transaction and ends with one `PASS` row per check: `admin_action` accepted, unknown event types rejected, deletes still blocked, and callback matching still allowed. Without the migration, resolving an order fails with a database error.

### Invites and password reset migration (Module 6 addendum)

Admin invitations and email password resets need migration `20261016000100_admin_invites_and_password_resets.sql`. It adds `users.password_changed_at` and the `admin_invites` and `password_reset_codes` tables (row level security on, no access for `anon` or `authenticated`). `supabase db reset` (local) applies it with the others; for a hosted project run `supabase db push` as above, then run `supabase/verification/invites_check.sql` in the Supabase SQL Editor. It works inside a rolled-back transaction and ends with one `PASS` row per check.

Then install the new dependency (`python -m pip install -r requirements.txt`, which adds `aiosmtplib`), set `INVITE_CODE_PEPPER` (generate it like `JWT_SECRET` below), and configure email as described in `docs/email.md`. In development the default `EMAIL_PROVIDER=console` writes emails to `./.dev_mailbox/`. Invite admins with `POST /api/v1/admin/invites`; see `docs/admin.md`.

### Google sign in migration

"Continue with Google" for farmers and buyers needs migration `20261027000100_google_sign_in.sql`. It is additive and safe on a populated database: it adds `users.password_set` (true for every existing account) and the `user_identities`, `oauth_nonces` and `google_signups` tables (row level security on, no access for `anon`, `authenticated` or `pipeline_writer`), and schedules the `cleanup-google-auth` job. `supabase db reset` (local) applies it with the others; for a hosted project run `supabase db push` as above, then run `supabase/verification/google_check.sql` in the Supabase SQL Editor (rolled-back transaction, one `PASS` row per check). Apply it before starting this version of the API, because the user model now reads `password_set`.

Then reinstall the requirements (`python -m pip install -r requirements.txt`; `PyJWT[crypto]` and `cryptography` verify Google's RS256 signatures), create the Google OAuth client as described in [docs/google-setup.md](docs/google-setup.md) and set `GOOGLE_CLIENT_ID`. Without it Google sign in stays off and everything else works.

## 5. Create the initial administrator (optional)

After the database migrations have been applied, set `SEED_ADMIN_NAME` and `SEED_ADMIN_EMAIL` in `.env` (accounts have no phone number; `SEED_ADMIN_PHONE` is no longer read). Either set `SEED_ADMIN_PASSWORD` to a password of at least 12 characters or leave it blank to be prompted twice without echoing it. Then run once per environment:

```bash
python -m scripts.seed_admin
```

The script exits without changes if a super admin already exists. Remove `SEED_ADMIN_PASSWORD` from `.env` after a successful seed. Do not run the price seed script until its FEWS file and product mappings have been configured in `scripts/seed_prices.py`.

Accounts are identified by a verified email only (identity v2). Farmers and buyers register with name, email and password and confirm the email with an emailed code before the account exists. Every password sign in for the roles in `LOGIN_OTP_ROLES` (default all three) needs a second step: a code emailed after the password is verified. So the first super admin sign in already needs email delivery: use `EMAIL_PROVIDER=console` in development (the code is written to `.dev_mailbox/`), or the recovery script `python -m scripts.admin_recovery issue-login-code --email <email>` (requires `ALLOW_ADMIN_RECOVERY=1`; see [docs/admin.md](docs/admin.md)). Moving an existing project to identity v2 wipes its data: follow [docs/identity-v2-cutover.md](docs/identity-v2-cutover.md).

Farmer/buyer registration and password changes require 8-128 characters, including an uppercase letter, lowercase letter, digit, and non-whitespace symbol. Sign in continues to accept existing passwords so users are not locked out by policy changes.

## 6. Run the API

With the virtual environment active, from the repository root, use the same command on Windows, macOS, and Linux:

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Keep this terminal open while using the API. Stop the development server with `Ctrl+C`.

The unauthenticated process health check is `http://127.0.0.1:8000/api/v1/health`. The database health check, `http://127.0.0.1:8000/api/v1/health/db`, returns `503` with `{"status":"degraded"}` if the database is unavailable.

## 7. Open Swagger

With the API running and `APP_ENV=development`, open **http://127.0.0.1:8000/docs**. ReDoc is at **http://127.0.0.1:8000/redoc**. Interactive docs are disabled when `APP_ENV=production`.

To call protected endpoints, sign in with `POST /api/v1/auth/login` (email and password), read the code from the email (with `EMAIL_PROVIDER=console`, the newest file in `.dev_mailbox/`), and send it with the `challenge_token` to `POST /api/v1/auth/login/verify`. Click **Authorize** in Swagger and enter the returned access token. The `/auth/register/farmer` and `/auth/register/buyer` routes start a registration that `/auth/register/verify` completes with the emailed code. An admin must be created with the seed step above.

## 8. M-Pesa Daraja sandbox and ngrok setup

Payments use the Safaricom Daraja **sandbox** only (shortcode `174379`); no real money moves. The API starts without these settings, and `POST /orders/{id}/pay` returns 503 `payments_unavailable` until they are complete. Daraja sends the payment result to a public HTTPS callback URL, so during local development an ngrok tunnel forwards that URL to the API on port 8000.

### 8.1 Create a Daraja sandbox app

1. Sign up and log in at https://developer.safaricom.co.ke.
2. Open **My Apps**, create a sandbox app, and enable **Lipa Na M-Pesa Sandbox** (M-Pesa Express).
3. Copy the app's **Consumer Key** and **Consumer Secret**.
4. Open the M-Pesa Express simulator or **Test Credentials** page and copy the sandbox **Passkey** for shortcode `174379`.

### 8.2 Install ngrok

Create a free account at https://dashboard.ngrok.com/signup first.

Windows PowerShell:

```powershell
winget install ngrok.ngrok
```

macOS (Homebrew):

```bash
brew install ngrok
```

Linux (snap; on other distributions download the agent from https://ngrok.com/download):

```bash
sudo snap install ngrok
```

Close and reopen the terminal (on Windows, restart VS Code completely) so the new PATH entry is picked up, then check the version on any OS:

```bash
ngrok version
```

Free accounts need agent version **3.20.0 or newer**; older agents fail with `ERR_NGROK_121`. WinGet can install an old version, so update in place if needed (same command on every OS):

```bash
ngrok update
```

### 8.3 Connect ngrok to your account and claim a static domain

1. Copy your authtoken from https://dashboard.ngrok.com/get-started/your-authtoken and save it (same command on every OS):

   ```bash
   ngrok config add-authtoken <your-ngrok-authtoken>
   ```

2. In the ngrok dashboard, open **Domains** and claim the free static domain, for example `your-name.ngrok-free.app`. It stays the same on every restart, so `.env` only needs setting once.

### 8.4 Generate the callback token

The callback token is the secret path segment that proves a callback came through your configured URL. Generate one with the activated virtual environment (same command on every OS):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Treat it like a password: anyone with the full callback URL can send fake callbacks.

### 8.5 Configure `.env`

Set these values in `.env` (never in `.env.example`):

```dotenv
MPESA_ENV=sandbox
MPESA_BASE_URL=https://sandbox.safaricom.co.ke
MPESA_CONSUMER_KEY=<consumer key from 8.1>
MPESA_CONSUMER_SECRET=<consumer secret from 8.1>
MPESA_SHORTCODE=174379
MPESA_PASSKEY=<sandbox passkey from 8.1>
MPESA_CALLBACK_BASE_URL=https://your-name.ngrok-free.app
MPESA_CALLBACK_TOKEN=<token from 8.4>
MPESA_STK_TARGET=test_msisdn
MPESA_TEST_MSISDN=254708374149
```

- `MPESA_CALLBACK_BASE_URL` must start with `https://` and has no trailing path; the API appends `/api/v1/payments/callback/<token>` itself.
- `MPESA_STK_TARGET=test_msisdn` sends every prompt to the sandbox test number. Use `entered` (the `.env.example` default) to send it to the M-Pesa number the buyer enters when paying instead. `registered` is still accepted as a deprecated alias of `entered` (the API logs a warning at startup); prefer `entered`.
- The other `MPESA_*` limits are optional; see [Environment reference](#environment-reference).

### 8.6 Start the API and the tunnel

Terminal 1, the API (activate the virtual environment first, as in step 2):

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Terminal 2, the tunnel (same command on every OS):

```bash
ngrok http --url=your-name.ngrok-free.app 8000
```

ngrok shows `Forwarding https://your-name.ngrok-free.app -> http://localhost:8000`. Only one tunnel can use the domain at a time. Restart the API after every `.env` change, because settings are cached.

### 8.7 Check the tunnel

Windows PowerShell:

```powershell
Invoke-RestMethod https://your-name.ngrok-free.app/api/v1/health -Headers @{"ngrok-skip-browser-warning" = "1"}
```

macOS or Linux:

```bash
curl -H "ngrok-skip-browser-warning: 1" https://your-name.ngrok-free.app/api/v1/health
```

Both should return `{"status":"ok"}`. In a browser, the free domain first shows an ngrok warning page; Daraja's server-to-server callbacks are not affected by it.

### 8.8 Make a test payment

1. Open http://127.0.0.1:8000/docs, sign in as a buyer with `POST /api/v1/auth/login` and `POST /api/v1/auth/login/verify` (the code is emailed; with `EMAIL_PROVIDER=console` it is in `.dev_mailbox/`), and click **Authorize** with the access token.
2. Create an order with `POST /api/v1/orders`. An order expires after `ORDER_EXPIRY_MINUTES` (default 5), and a payment must start with at least 90 seconds left.
3. Call `POST /api/v1/orders/{id}/pay` with an `Idempotency-Key` header (8 to 64 characters of `A-Z a-z 0-9 _ -`, for example `test-pay-0001`) and the body `{"phone_number": "0708374149"}` (required; accounts have no phone number). Use a new key for each new attempt; repeating a key with the same number replays the first response.

   The same call from a terminal, with `$TOKEN`/`$ORDER_ID` replaced by your values.

   Windows PowerShell:

   ```powershell
   Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/orders/$ORDER_ID/pay" -Headers @{ Authorization = "Bearer $TOKEN"; "Idempotency-Key" = "test-pay-0001" } -ContentType "application/json" -Body '{"phone_number": "0708374149"}'
   ```

   macOS or Linux:

   ```bash
   curl -X POST "http://127.0.0.1:8000/api/v1/orders/$ORDER_ID/pay" -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: test-pay-0001" -H "Content-Type: application/json" -d '{"phone_number": "0708374149"}'
   ```

4. A 202 response contains `"prompt": "Prompt sent to 254*******149"`. Poll `GET /api/v1/orders/{id}`: the payment moves from `initiated` to `completed` and the order to `paid` when Daraja calls back.
5. Watch the callback arrive in the ngrok inspector at http://127.0.0.1:4040 and in the `payments` and `payment_events` tables. The API log shows the callback path as `/api/v1/payments/callback/***`, never the token.

If no callback arrives, the next `GET /orders/{id}` after `RECONCILE_AFTER_SECONDS` (default 90) asks Daraja for the status instead. If the sandbox never calls back, send a simulated callback using the payment's `checkout_request_id` from the `payments` table (same commands on every OS):

```bash
python -m scripts.simulate_callback --checkout-request-id ws_CO_... --amount <order total>
python -m scripts.simulate_callback --checkout-request-id ws_CO_... --result-code 1032
```

The script reads the token from `.env`, refuses to run unless `MPESA_ENV=sandbox` and `APP_ENV` is not `production`, and never prints the token. A simulated callback is not a Daraja result: never present it as a real M-Pesa confirmation in a demonstration.

### 8.9 Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ngrok` is not recognized | The terminal started before ngrok was installed. Reopen it (on Windows, restart VS Code completely). |
| `ERR_NGROK_121` (agent version too old) | Run `ngrok update`, then `ngrok version` should show 3.20.0 or newer. |
| `You must specify a single argument` | Pass the domain with `--url=` and only the port as the argument: `ngrok http --url=your-name.ngrok-free.app 8000`. |
| `ERR_NGROK_334` (endpoint already online) | Another tunnel is using the domain. Stop it, or end it in the ngrok dashboard. |
| 503 `payments_unavailable` | A Daraja credential, the callback token, or an `https://` `MPESA_CALLBACK_BASE_URL` is missing. Fix `.env` and restart the API. |
| 409 `order_expiring` | Fewer than 90 seconds remain on the order. Create a new order. |
| 502 `payment_request_failed` | Daraja rejected the request or timed out. Check the credentials and passkey, wait a few seconds, and retry with a new `Idempotency-Key`. |
| Payment stays `initiated` | No callback arrived. Check that the tunnel is running and the inspector shows the request; poll `GET /orders/{id}` to trigger a status query, or use the simulator. |

## Tests and code checks

With the virtual environment active:

```bash
python -m pytest -q
ruff check app scripts tests
ruff format --check app scripts tests
```

Database tests are marked `db`, skipped by default, and refuse to run when `APP_ENV=production`. The normal test suite uses fake sessions and does not connect to Supabase.

To run the database tests, point `DATABASE_URL` at a migrated local database (never a shared or hosted one) and opt in:

```bash
RUN_DB_TESTS=1 python -m pytest -q -m db
```

In Windows PowerShell, set the variable first with `$env:RUN_DB_TESTS = "1"`. Each database test runs inside a transaction that is rolled back, so no rows are left behind.

### Checkout concurrency check (manual)

`scripts/check_checkout_concurrency.py` proves that concurrent buyers cannot reserve the same stock. It is not part of `pytest` and must be run by hand:

```bash
ALLOW_CONCURRENCY_CHECK=1 python -m scripts.check_checkout_concurrency
```

In Windows PowerShell, set `$env:ALLOW_CONCURRENCY_CHECK = "1"` first. **Warning:** the script writes committed rows to the database in `DATABASE_URL`, so use only a local or disposable database. It refuses to run when `APP_ENV=production`. It deletes the orders and listings it creates, but users cannot be deleted, so six disabled `ZZ_TEST_` users remain after each run. It prints PASS or FAIL per scenario and exits non-zero on failure.

### Contract, inventory and security checks

The normal `pytest` run also checks that:

- every route is listed once in `tests/route_matrix.py` (the authorization inventory), and every role outside a route's allowed set gets 401 or 403;
- every route has one `### METHOD /api/v1/path` heading in `docs/api.md`;
- `docs/openapi.json` matches the application (`tests/test_openapi_snapshot.py`). After changing any route or schema, regenerate it with `python scripts/export_openapi.py` and commit the file;
- the sources in `app/` and `scripts/` contain no formatted SQL in `text()`, no `eval`, `pickle`, `subprocess`, disabled TLS checks, `print` in `app/`, float money, secret-looking literals or production Daraja options (`tests/test_source_security.py`).

CI (`.github/workflows/ci.yml`) runs `ruff check .` and `pytest -q -m "not db"` on every push to `main` and every pull request, plus a non-blocking `pip-audit` job. It uses no secrets.

### Scripts

| Script | Purpose |
| --- | --- |
| `python scripts/check_config.py` | Checks the current environment's settings (OK, MISSING, WEAK, INVALID per area) without printing any value; exit code 1 when anything needs attention. |
| `python scripts/export_openapi.py` | Writes `docs/openapi.json`, the frontend contract. |
| `python scripts/e2e_check.py` | Manual end-to-end check of the farmer, buyer and admin journeys against a running server. See [docs/testing.md](docs/testing.md). |
| `python -m scripts.seed_demo_data` | Creates demo farmers, buyers and listings for frontend work (never in production). |
| `python -m scripts.seed_admin` | One-time super admin seed (name, email, password; the first sign in needs an emailed code). |
| `python -m scripts.admin_recovery reset-password --email <email>` / `issue-login-code --email <email>` | Operator recovery for administrators when email is down; refuses unless `ALLOW_ADMIN_RECOVERY=1`. See [docs/admin.md](docs/admin.md). |

## Documentation

- [docs/api.md](docs/api.md): endpoint reference.
- [docs/frontend_contract.md](docs/frontend_contract.md): what the frontend repository needs (auth, errors, screens, payments, uploads, type generation).
- [docs/security.md](docs/security.md): security controls and known limitations.
- [docs/testing.md](docs/testing.md): unit, database, concurrency and end-to-end checks.
- [docs/backup.md](docs/backup.md): logical backups with `pg_dump`.
- [docs/deployment.md](docs/deployment.md): deployment checklist.
- [docs/ci.md](docs/ci.md): what the CI pipeline checks.
- [docs/identity-v2-cutover.md](docs/identity-v2-cutover.md): moving a project to email only accounts (wipes the data; the exact steps).
- [docs/google-setup.md](docs/google-setup.md): creating the Google OAuth client for "Continue with Google".
- [docs/decisions.md](docs/decisions.md), [docs/schema.md](docs/schema.md), [docs/payments.md](docs/payments.md), [docs/admin.md](docs/admin.md), [docs/email.md](docs/email.md).

## Implemented scope

Authentication, health checks, market/price/forecast reads, farmer-owned listing creation/edit/removal, farmer listing/order history, buyer listing browse/detail, checkout with stock reservation, buyer order history/detail/cancel, and M-Pesa sandbox payments (pay endpoint, callback, and reconciliation; see `docs/payments.md`) and the admin APIs (admin invitations by emailed code, users, listing moderation, order and payment review, reconciliation, statistics and forecast accuracy; see `docs/admin.md`) have service implementations. Forecast and price data must be populated by the separate ML pipeline or configured FEWS import before they return useful results. Only markets with price observations are offered (currently Nairobi and Kisumu).

Frontend support additions: `GET /api/v1/listings/mine/{listing_id}` (farmer reads one own listing in any status, for the edit screen), `GET /api/v1/listings/markets` (buyer browse market filter: markets with buyer-visible listings and their counts, no price data), and `is_super_admin` in the login response and the user profile (`GET /auth/me`, registration, accept-invite), read from the database and never put in the token. These change the API schema, so `docs/openapi.json` must be re-exported with `python scripts/export_openapi.py` (until then `tests/test_openapi_snapshot.py` fails).

## Production start

Run behind a trusted reverse proxy that sets `X-Forwarded-For`. Restrict `--forwarded-allow-ips` to trusted proxy addresses in deployments where they are known; `*` trusts all sources and should only be used when the app is not directly reachable by untrusted clients.

```bash
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="*"
```

Before deploying, run `python scripts/check_config.py` in the target environment and follow [docs/deployment.md](docs/deployment.md).

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
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime, from 5 to 1440 minutes (default 480, 8 hours). |
| `CORS_ORIGINS` | Comma-separated allowed frontend origins, each `scheme://host[:port]` with no path or trailing slash; no `*` in production. |
| `LOGIN_MAX_FAILURES` | Failed sign in attempts allowed per email per window. |
| `LOGIN_FAILURE_WINDOW_MINUTES` | Per email throttle window. |
| `LOGIN_OTP_ROLES` | Roles that need an emailed code after the password: comma list of `farmer`, `buyer`, `admin` (default all three). Production refuses a list without `admin`. |
| `LOGIN_CODE_TTL_MINUTES` / `LOGIN_CHALLENGE_TTL_MINUTES` | Lifetime of a sign in code and of its challenge, 5 to 60 (default 10 and 10; the code cannot outlive the challenge). |
| `LOGIN_MAX_ATTEMPTS` | Wrong sign in codes before the challenge locks, 1 to 20 (default 5). |
| `LOGIN_MAX_SENDS` | Sign in codes sent per challenge, 1 to 5 (default 3). |
| `LOGIN_RESEND_COOLDOWN_SECONDS` | Gap between sign in code emails, 10 to 3600 (default 60). |
| `LOGIN_CHALLENGES_PER_HOUR` | Sign in challenges per user per hour, 1 to 100 (default 10). |
| `REG_TTL_MINUTES` | Lifetime of a pending registration, 10 to 1440 (default 60). |
| `REG_CODE_TTL_MINUTES` | Lifetime of a registration code, 5 to 60 (default 15). |
| `REG_MAX_ATTEMPTS` | Wrong registration codes before the registration locks, 1 to 20 (default 5). |
| `REG_MAX_SENDS` | Registration codes sent per registration, 1 to 5 (default 5). |
| `REG_RESEND_COOLDOWN_SECONDS` | Gap between registration code emails, 10 to 3600 (default 60). |
| `REG_EXISTS_NOTICE_MINUTES` | Minimum gap between "account exists" emails for one address, per process, 1 to 1440 (default 10). |
| `GOOGLE_CLIENT_ID` | Google OAuth Web client ID for "Continue with Google" (farmers and buyers), ending with `.apps.googleusercontent.com`. Empty (the default) turns Google sign in off: its routes answer 503 `google_unavailable`. See [docs/google-setup.md](docs/google-setup.md). |
| `GOOGLE_NONCE_TTL_SECONDS` | Lifetime of a Google sign in nonce, 60 to 900 (default 300). |
| `GOOGLE_SIGNUP_TTL_MINUTES` | Lifetime of the ticket that waits for a new Google user's role choice, 2 to 30 (default 10). |
| `GOOGLE_JWKS_CACHE_SECONDS` | How long Google's signing keys are cached (a shorter `Cache-Control: max-age` wins), 60 to 86400 (default 3600). |
| `GOOGLE_CLOCK_SKEW_SECONDS` | Clock skew allowed when checking Google ID token times, 0 to 300 (default 60). |
| `GOOGLE_MAX_TOKEN_AGE_SECONDS` | Oldest accepted Google ID token (`iat`), 60 to 3600 (default 900). |
| `MPESA_ENV` | Must remain `sandbox`. |
| `MPESA_BASE_URL` | Must remain `https://sandbox.safaricom.co.ke`. |
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` | Daraja sandbox OAuth credentials. |
| `MPESA_SHORTCODE` / `MPESA_PASSKEY` | Daraja sandbox STK credentials. |
| `MPESA_CALLBACK_BASE_URL` / `MPESA_CALLBACK_TOKEN` | Public callback base URL and secret path token. |
| `MPESA_STK_TARGET` | `entered` (default: the number the buyer enters when paying), `test_msisdn` (every prompt to `MPESA_TEST_MSISDN`), or `registered` (deprecated alias of `entered`). |
| `MPESA_TEST_MSISDN` | Sandbox test phone used by `test_msisdn` mode (format `254[17]XXXXXXXX`). |
| `MPESA_MAX_ATTEMPTS_PER_ORDER` | Payment attempts allowed per order (default 3). |
| `MPESA_MIN_SECONDS_BEFORE_EXPIRY` | Seconds that must remain before order expiry to start a payment (default 90). |
| `MPESA_STALE_INITIATING_SECONDS` | Age after which an unsent `initiating` payment is failed (default 60). |
| `MPESA_PHONE_ATTEMPTS_PER_HOUR` | Payments allowed per entered number per hour, across all buyers (default 5). |
| `MPESA_BUYER_ATTEMPTS_PER_HOUR` | Payments allowed per buyer per hour across all orders (default 10, 1 to 100). |
| `MPESA_BUYER_DISTINCT_PHONES_PER_DAY` | Different numbers a buyer may pay with in 24 hours (default 3, 1 to 20). |
| `MPESA_QUERY_MIN_INTERVAL_SECONDS` | Minimum gap between STK status queries for one payment (default 20). |
| `MPESA_TIMEOUT_RECONCILE_WINDOW_MINUTES` | How long a timed-out payment is still queried for a late success (default 15). |
| `MPESA_TRANSACTION_DESC` | STK `TransactionDesc`, at most 13 characters (default `Crop order`). |
| `CALLBACK_MAX_BYTES` | Largest accepted callback body (default 16384). |
| `RESOLVE_NOTE_MIN_LENGTH` | Minimum length of the note when resolving a reconciliation order (default 10). |
| `ORDER_EXPIRY_MINUTES` | Pending order lifetime and stock reservation, 2 to 30 minutes (default 5). |
| `RECONCILE_AFTER_SECONDS` | Age after which order reads query Daraja. |
| `MAX_IMAGE_BYTES` | Maximum listing image upload size. |
| `MAX_ACTIVE_LISTINGS_PER_FARMER` | Maximum active listings per farmer (default 50). |
| `IMAGE_MAX_DIMENSION` | Maximum longest side of stored listing images (default 1600 pixels). |
| `IMAGE_JPEG_QUALITY` | JPEG output quality (default 85). |
| `MIN_ORDER_QUANTITY` | Smallest order quantity in kg (default 1). |
| `MIN_ORDER_TOTAL_KES` | Smallest order total in shillings (default 10). |
| `MAX_PENDING_ORDERS_PER_BUYER` | Unexpired pending orders a buyer may hold (default 3). |
| `ORDER_REFERENCE_LENGTH` | Order reference length, 6 to 12 characters (default 8). |
| `SEED_ADMIN_NAME`, `SEED_ADMIN_EMAIL` | Initial super-admin profile (`SEED_ADMIN_PHONE` was removed). |
| `SEED_ADMIN_PASSWORD` | Optional initial password, at least 12 characters; remove after seeding. |
| `DEMO_EMAIL_BASE` | `scripts/seed_demo_data.py` only: `you@gmail.com` gives demo addresses like `you+demo-farmer1@gmail.com`; empty gives `demo.farmer1@example.com` style addresses. |
| `ALLOW_ADMIN_RECOVERY` | Process environment only: `1` lets `scripts/admin_recovery.py` run. Never set it on the server. |
| `INVITE_CODE_PEPPER` | Secret (at least 32 characters) used to HMAC every email code: registration, sign in, invites and resets (the name is historical). Without it those routes return 503 `codes_unavailable`, so nobody in `LOGIN_OTP_ROLES` can sign in; the rest of the API works. |
| `INVITE_TTL_DAYS` | Days an admin invite stays open, 1 to 30 (default 7). |
| `INVITE_CODE_TTL_MINUTES` | Minutes an emailed invite code works, 5 to 60 (default 15). |
| `INVITE_MAX_ATTEMPTS` | Wrong codes in total before an invite locks, 1 to 20 (default 10). |
| `INVITE_MAX_SENDS` | Codes sent per invite, 1 to 5 (default 5). |
| `INVITE_RESEND_COOLDOWN_SECONDS` | Minimum gap between code emails for one invite or account, 10 to 3600 (default 60). |
| `INVITE_MAX_PENDING` | Pending invites allowed at once, 1 to 100 (default 10). |
| `RESET_CODE_TTL_MINUTES` | Minutes a password reset code works, 5 to 60 (default 15). |
| `RESET_MAX_ATTEMPTS` | Wrong codes before a reset code locks, 1 to 20 (default 5). |
| `RESET_MAX_REQUESTS_PER_HOUR` | Reset codes issued per account per hour, 1 to 20 (default 3). |
| `EMAIL_PROVIDER` | `console` (development only, refused in production), `smtp`, `resend` or `brevo`. See `docs/email.md`. |
| `EMAIL_API_KEY` | Resend or Brevo API key; required for those providers. |
| `EMAIL_FROM_NAME` | Sender display name (default `Admin via AgriSence`). |
| `EMAIL_FROM_ADDRESS` | Sender address; required for `smtp`, `resend` and `brevo`. |
| `EMAIL_REPLY_TO` | Optional reply address. |
| `FRONTEND_BASE_URL` | Web app base URL used in emails (`/accept-invite`, `/reset-password`); must start with `https://` in production. |
| `SMTP_HOST`, `SMTP_PORT` | SMTP server (default `smtp.gmail.com`, `587`). |
| `SMTP_SECURITY` | `starttls` (port 587, default) or `ssl` (port 465). Certificate verification is always on. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | SMTP login; for Gmail the account address and an app password, never the normal password. |
| `SMTP_TIMEOUT_SECONDS` | SMTP connection and command timeout, 1 to 120 (default 15); also bounds the synchronous sign in code email for every provider. |
| `LOG_FORMAT` | `text` (default) or `json` (one JSON object per line with `time`, `level`, `logger`, `message`, `request_id`, `method`, `path`, `status`, `duration_ms`). |
| `MAX_JSON_BODY_BYTES` | Largest JSON or form request body, 1024 to 10485760 (default 65536); larger bodies get 413 `payload_too_large`. The two multipart listing routes allow `MAX_IMAGE_BYTES` + 64 KiB. |
| `DB_STATEMENT_TIMEOUT_MS` | `SET LOCAL statement_timeout` for every transaction, 0 (off) or 100 to 600000 (default 15000). |
| `DB_LOCK_TIMEOUT_MS` | `SET LOCAL lock_timeout` for every transaction, 0 (off) or 100 to 600000 (default 5000). |
| `ALLOWED_HOSTS` | Comma-separated host names accepted in the `Host` header. Empty means `*` outside production; required (no `*`) in production. |
| `RATE_LIMITS_DISABLED` | `true` turns the in-memory rate limits off for development and the end-to-end script; refused in production (default `false`). |

## Payments and credentials

Payments are sandbox-only. Configuration rejects any non-sandbox Daraja environment or URL; no real money is transferred. Never commit `.env`, passwords, access tokens, API keys, passkeys, database URLs, or service-role credentials.

The app starts without payment settings; the pay endpoint then returns 503 `payments_unavailable`. See [8. M-Pesa Daraja sandbox and ngrok setup](#8-m-pesa-daraja-sandbox-and-ngrok-setup) to enable payments, and `docs/payments.md` for the payment design.
