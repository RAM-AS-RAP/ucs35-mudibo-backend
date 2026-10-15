# Testing

Run everything from the repository root with the virtual environment active (`.\.venv\Scripts\Activate.ps1` on Windows).

| Check | Command | Needs | Guarantees |
| --- | --- | --- | --- |
| Unit and API tests | `python -m pytest -q` | Nothing (no database, no network) | Every route's behaviour with fake sessions and dependency overrides; the hardening checks below. |
| Lint | `ruff check .` | Nothing | Style, imports, and simple errors. |
| Database tests | `RUN_DB_TESTS=1 python -m pytest -q -m db` | A local migrated database | SQL, triggers, constraints, locks and views against real PostgreSQL. |
| Checkout concurrency | `ALLOW_CONCURRENCY_CHECK=1 python -m scripts.check_checkout_concurrency` | A local or disposable database | Concurrent buyers can never reserve the same stock twice. |
| End-to-end | `python scripts/e2e_check.py` | A running server | The farmer, buyer and admin journeys work over real HTTP. |
| Configuration | `python scripts/check_config.py` | The target environment's variables | Every setting is present and acceptable, without printing any value. |

CI (`.github/workflows/ci.yml`) runs `ruff check .` and `pytest -q -m "not db"` on every push to `main` and every pull request, plus a non-blocking `pip-audit` job.

## Unit and API tests

```bash
python -m pytest -q
python -m pytest tests/test_hardening.py -q        # one file
```

They build the app with `create_app()` and call it through `httpx.AsyncClient` and `ASGITransport`, with fake sessions. They never connect to Supabase or Daraja. Besides the module tests, they include:

- `tests/test_route_inventory.py` checks that every route of the application is in `tests/route_matrix.py`, and the reverse. Every role outside a route's allowed set gets 401 (anonymous) or 403, before validation and without touching the database.
- `tests/test_docs_drift.py` checks that every route has exactly one `### METHOD /api/v1/path` heading in `docs/api.md`.
- `tests/test_openapi_snapshot.py` checks that `docs/openapi.json` equals the application's schema. When it fails (or skips because the file is missing), run `python scripts/export_openapi.py` and commit the file.
- `tests/test_source_security.py` statically scans `app/` and `scripts/` for unsafe patterns (see [security.md](security.md)).
- `tests/test_hardening.py` covers request ids, error bodies, body limits, security headers, CORS, allowed hosts, rate limits, the new settings and database error mapping.
- `tests/test_logging_redaction.py` runs a login, a payment and an invite flow and checks that no password, token, code, Daraja secret or full phone number reaches the logs.
- `tests/test_check_config.py` tests the configuration checker with fake environments.

## Database tests

Point `DATABASE_URL` at a **local** migrated database (`supabase start` and `supabase db reset`, then `postgresql://postgres:postgres@127.0.0.1:54322/postgres`). Never use a shared or hosted database.

```bash
RUN_DB_TESTS=1 python -m pytest -q -m db
```

In PowerShell: `$env:RUN_DB_TESTS = "1"; python -m pytest -q -m db`. Each test runs in a transaction that is rolled back. The suite refuses to run when `APP_ENV=production`. The application engine applies `SET LOCAL statement_timeout` and `lock_timeout` at the start of each transaction. Inside the rolled-back test transaction this changes nothing that survives the test.

## Checkout concurrency script

```bash
ALLOW_CONCURRENCY_CHECK=1 python -m scripts.check_checkout_concurrency
```

It writes committed rows, so use only a local or disposable database. It refuses `APP_ENV=production`, prints PASS or FAIL per scenario, deletes its orders and listings, and leaves six disabled `ZZ_TEST_` users (users cannot be deleted).

## End-to-end check (`scripts/e2e_check.py`)

A manual script, never part of `pytest`. It exercises the three user journeys over HTTP against a running server.

1. Start the server with rate limits off. The run registers and logs in several times, and the normal limits (registration 10 per hour, login 5 per minute per IP) would otherwise stop it:

   ```bash
   RATE_LIMITS_DISABLED=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

   For `E2E_PAYMENTS=simulate`, also start it with `MPESA_STK_TARGET=test_msisdn`. For `E2E_INVITES=console`, use `EMAIL_PROVIDER=console`.

2. Run the script in another terminal:

   ```bash
   E2E_ADMIN_IDENTIFIER=<super admin phone or email> E2E_ADMIN_PASSWORD=<password> python scripts/e2e_check.py
   ```

| Variable | Default | Meaning |
| --- | --- | --- |
| `E2E_BASE_URL` | `http://localhost:8000` | Server to test. |
| `E2E_ALLOW_REMOTE` | unset | `1` allows a host other than localhost (for example a deployed API). |
| `E2E_ADMIN_IDENTIFIER`, `E2E_ADMIN_PASSWORD` | unset | The super admin. Without them, or while the super admin still has to change the seeded password, the admin, invite and cleanup steps are skipped and the e2e users stay enabled. |
| `E2E_ORIGIN` | first `CORS_ORIGINS` entry | An origin the server allows, for the CORS preflight check. |
| `E2E_PAYMENTS` | `skip` | `skip`, `simulate` (sandbox STK push, then a simulated success callback posted by the script; needs `DATABASE_URL` and `MPESA_CALLBACK_TOKEN`), or `prompt` (waits up to 150 s for someone to approve the prompt; needs `E2E_CONFIRM_REAL_PROMPT=yes` because the prompt may reach a real phone). |
| `E2E_INVITES` | `skip` | `console`: create an invite, read the code from the newest file in `.dev_mailbox/` (same machine), accept it, log in as the new admin, check a repeated accept gives the generic 400, then disable that admin. |
| `DATABASE_URL`, `MPESA_CALLBACK_TOKEN` | unset | Only for `simulate`: the script reads the payment's checkout request id (one read-only query) and posts the callback with the token. |

What it checks:

- System: health, database health, CORS for an allowed and a disallowed origin, the uniform 404 body, and `X-Request-ID` plus `Cache-Control: no-store` on every response.
- Farmer: registration with `role` in the body is rejected, then register, login, `GET /auth/me`, markets, current prices, trends, and forecasts (SKIP when none exist yet). Then listings with and without a photo, the photo URL, quantity 0 and back, delete, and 403 on buyer routes.
- Buyer: 403 on prices and forecasts, browsing with filters and sorting, detail, an order with the server total, 409 `pending_order_exists`, cancel with the stock returned, a new order, and the order list and detail.
- Payments: per `E2E_PAYMENTS`. In `simulate` it also checks that replaying the idempotency key changes nothing, that the farmer sees the paid order without buyer contact details, and that a duplicate callback changes nothing.
- Admin: stats, filtered users, listings, orders, payments, and forecast accuracy. Farmers and buyers get 403 on admin routes. After the e2e users are disabled, their next requests get 401 and the disabled farmer's listing is hidden.
- Invites (optional) as above.
- Cleanup (always, even after a failure): cancel pending orders, remove the e2e listings, and disable the e2e users. It prints how many disabled `ZZ_E2E_` users remain, because users cannot be deleted.

Test users are named `ZZ_E2E_<random>` and use `2541` + 8 random digits as phone numbers. Passwords are generated per run. The script never prints tokens, passwords, codes or full phone numbers. It refuses to run when `APP_ENV` is `production`, or against a non-local host unless `E2E_ALLOW_REMOTE=1`. It prints a PASS/FAIL/SKIP table and exits with 1 when any step failed.

## Demo data (`scripts/seed_demo_data.py`)

```bash
python -m scripts.seed_demo_data
```

Creates 2 demo farmers and 2 demo buyers (`Demo …`, fixed phone numbers in the script) and about 8 maize and beans listings in the markets that have prices. It needs prices in `market_prices` and stops with a message when there are none. New passwords are printed once and never again. Running it again changes nothing. It refuses `APP_ENV=production`. The demo phone numbers are not real, so demo buyers should pay only while the server runs with `MPESA_STK_TARGET=test_msisdn`. It guarantees nothing by itself; it only provides realistic data for frontend development and demonstrations.
