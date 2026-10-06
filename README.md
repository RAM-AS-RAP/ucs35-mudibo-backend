# Mudibo Crop Price API

FastAPI backend for a SHAP-explainable crop-price decision support and farmer-buyer marketplace platform for smallholder farmers in Kenya. Forecasts and observed prices are read from PostgreSQL; the monthly ML pipeline is maintained separately.

- ML repository: https://github.com/RAM-AS-RAP/ucs35-mudibo-ml
- React PWA repository: https://github.com/RAM-AS-RAP/ucs35-mudibo-frontend

## Run locally

Use Python 3.12, create a local virtual environment, install `requirements.txt`, and configure a private `.env` from `.env.example`. Then run:

```powershell
uvicorn app.main:app --reload
```

Interactive API documentation is available at `/docs` outside production.

## Environment

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | Runtime environment; docs are disabled in production. |
| `DATABASE_URL` | Supabase PostgreSQL connection URL. |
| `SUPABASE_URL` | Supabase project URL used for Storage only. |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only Storage credential. |
| `SUPABASE_STORAGE_BUCKET` | Listing image bucket. |
| `JWT_SECRET` | Signing key for access tokens. |
| `JWT_ALGORITHM` | Access-token signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access-token lifetime. |
| `CORS_ORIGINS` | Comma-separated allowed frontend origins. |
| `MPESA_ENV` | Must be `sandbox`. |
| `MPESA_BASE_URL` | Must be `https://sandbox.safaricom.co.ke`. |
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` | Daraja sandbox OAuth credentials. |
| `MPESA_SHORTCODE` / `MPESA_PASSKEY` | Daraja sandbox STK credentials. |
| `MPESA_CALLBACK_BASE_URL` / `MPESA_CALLBACK_TOKEN` | Public callback base URL and secret path token. |
| `MPESA_STK_TARGET` | `registered` or `test_msisdn` for sandbox prompts. |
| `MPESA_TEST_MSISDN` | Sandbox test phone used by `test_msisdn` mode. |
| `ORDER_EXPIRY_MINUTES` | Pending order lifetime. |
| `RECONCILE_AFTER_SECONDS` | Age after which order reads query Daraja. |
| `MAX_IMAGE_BYTES` | Maximum listing image upload size. |
| `SEED_ADMIN_NAME`, `SEED_ADMIN_PHONE`, `SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD` | Initial super-admin seed inputs. |

## Database and seeding

Apply Supabase migrations first, then load `supabase/seed.sql`, run `python scripts/seed_admin.py` once per environment, and run `python scripts/seed_prices.py <fews-json-folder>` after its source mappings are configured. The SQL migrations own the schema; this API never calls `create_all`.

## Payments and credentials

Payments are sandbox-only. Configuration rejects any non-sandbox Daraja environment or URL; no real money is transferred. Never commit `.env`, passwords, access tokens, API keys, passkeys, or service-role credentials.