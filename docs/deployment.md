# Deployment checklist

A checklist only. This repository contains no deployment configuration on purpose. Work through it top to bottom for each deployment.

## Before deploying

- [ ] `python scripts/check_config.py` passes (exit code 0) **in the target environment**, with the production variables loaded.
- [ ] `APP_ENV=production`. This disables `/docs`, `/redoc` and `/openapi.json`, turns on HSTS, and enforces the production setting rules.
- [ ] `JWT_SECRET` and `INVITE_CODE_PEPPER` are **newly generated for production** (`python -c "import secrets; print(secrets.token_urlsafe(48))"`), never copied from development.
- [ ] `CORS_ORIGINS` is set to the real frontend origin(s), exact `https://host`, with no wildcard and no trailing slash.
- [ ] `ALLOWED_HOSTS` is set to the real API host name(s).
- [ ] `MPESA_CALLBACK_BASE_URL` is the public `https://` address of this API, and `MPESA_CALLBACK_TOKEN` is a new random token of at least 32 characters.
- [ ] `EMAIL_PROVIDER` is an HTTPS provider (`resend` or `brevo`) with `EMAIL_FROM_ADDRESS` set, and `FRONTEND_BASE_URL` is the `https://` frontend address. **Email is a hard dependency of sign in**: registration, invite acceptance, password reset and every sign in for the roles in `LOGIN_OTP_ROLES` send an email, so nobody can sign in when mail does not leave the host.
- [ ] `LOGIN_OTP_ROLES` includes `admin` (production refuses to start otherwise; the default is `farmer,buyer,admin`).
- [ ] `ACCESS_TOKEN_EXPIRE_MINUTES` is what you intend (default 480, 8 hours).
- [ ] `RATE_LIMITS_DISABLED` is unset or `false` (production refuses `true`).
- [ ] `DATABASE_URL` uses the Supabase transaction pooler (port 6543).
- [ ] All migrations in `supabase/migrations/` are pushed to the production project, and the checks in `supabase/verification/` pass (including `identity_v2_check.sql`). The identity v2 migration refuses a database that already has users; follow [identity-v2-cutover.md](identity-v2-cutover.md) for that change.
- [ ] The super admin is seeded (`python -m scripts.seed_admin`), has signed in once (password and emailed code), and has changed the initial password. `SEED_ADMIN_PASSWORD` has been removed from the environment. `ALLOW_ADMIN_RECOVERY` is not set.
- [ ] The ML pipeline's last run is green, and `GET /api/v1/admin/stats` shows current prices and forecasts (`pipeline.series[].forecast_current`).
- [ ] The Supabase project is not paused (free projects pause after a period of inactivity).
- [ ] A fresh backup has been taken ([backup.md](backup.md)).
- [ ] `docs/openapi.json` is up to date (`pytest` passes), and the frontend was built against it.

## Start command

Run one instance (see the caveats), behind the host's HTTPS proxy:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
```

`--proxy-headers` lets the rate limiter and logs see the client IP from `X-Forwarded-For`. Only use `--forwarded-allow-ips="*"` when the app is reachable solely through the host's proxy; otherwise list the proxy addresses. Set `LOG_FORMAT=json` if the host collects structured logs.

## After deploying

Run these in order, against the deployed address:

1. `GET /api/v1/health` returns `{"status": "ok"}`, and `GET /api/v1/health/db` returns `{"status": "ok"}`. Both responses have `X-Request-ID`, `Cache-Control: no-store` and `Strict-Transport-Security`.
2. `GET /docs` returns 404 (docs disabled).
3. Sign in as the super admin from the deployed frontend (password, then the emailed code). This also checks CORS from the real origin and that email leaves the host. If the code never arrives, fix email delivery; `scripts/admin_recovery.py issue-login-code` can let the operator in meanwhile.
4. Make one sandbox payment end to end with a test buyer: order, pay, approve the prompt on the sandbox test number, and see the order become `paid`. Check that the callback reached the API (the access log shows `/api/v1/payments/callback/***`).
5. `POST /api/v1/admin/email/test` as the super admin returns `{"email_sent": true}`, and the email arrives.
6. The end-to-end script reads codes from the console provider's mailbox, so its auth steps run only against a local server with `EMAIL_PROVIDER=console`; against a deployed API they are skipped with a reason. Run it locally before deploying:

   ```bash
   E2E_ADMIN_EMAIL=... E2E_ADMIN_PASSWORD=... python scripts/e2e_check.py
   ```

   Its cleanup disables the `ZZ_E2E_` users it created.

## Hosting caveats

- **Free instances sleep.** After a period without traffic, the first request can take 30 to 60 seconds while the instance wakes up. Wake the API before a demonstration, and expect Daraja callbacks to a sleeping instance to arrive late; order reads then reconcile the payment.
- **Free instances usually block outbound SMTP ports** (25, 465, 587), so `EMAIL_PROVIDER=smtp` will not deliver from them, and then nobody can register or sign in. Use an HTTPS email provider (`resend` or `brevo`) or a host that allows SMTP. Sign in codes are sent while the request waits, bounded by `SMTP_TIMEOUT_SECONDS`.
- **Rate limits are kept in process memory**, so run exactly one instance (one process, no autoscaling). With several instances, each would keep its own counters and the limits would multiply. Restarts reset the counters. The per email login throttle and the registration "account exists" notice throttle are in memory too.
