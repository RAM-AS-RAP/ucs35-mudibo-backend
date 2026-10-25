# Identity v2 cutover

How to move a Supabase project to identity v2 (email only accounts, emailed codes at registration and sign in). Read it fully before starting. The commands are run by the operator; nothing here runs automatically.

## Why the data is wiped

Identity v2 removes `phone_number` from `users` and `admin_invites`, makes `users.email` required and unique in lower case, and adds `email_verified_at` as a required column. Existing accounts would need a verified email that the database cannot produce (many farmers and buyers registered with a phone number only), and converting them by hand would invent verification that never happened. The project is a sandbox capstone deployment with demo data, so the database is reset instead: `supabase db reset --linked` drops the public schema and re-applies every migration, ending with `20261026000100_identity_v2.sql`.

As a safety net the identity v2 migration starts with a guard that raises "identity v2 requires an empty users table. Run supabase db reset --linked, then apply migrations." when `users` (or `admin_invites`) has rows, so it can never run on top of real accounts.

## What loses data

| Step | Data lost |
| --- | --- |
| 4. `supabase db reset --linked` | **Everything in the public schema**: users, listings, orders, payments, payment events, invites, reset codes, market prices and forecasts. The `pipeline_writer` role's password may also be lost. |
| 5. Storage check | Nothing is deleted, but uploaded listing photos in the `listing-images` bucket become orphans (Supabase managed schemas such as `storage` are not reset). |
| All other steps | No data is lost. |

Take a backup first if anything in the project matters ([backup.md](backup.md)).

## Steps, in order

1. **Confirm the code is ready.** In the backend repository run `python -m pytest -q` (the OpenAPI snapshot test fails until `docs/openapi.json` is regenerated with `python scripts/export_openapi.py`; do that first). Read `supabase/migrations/20261026000100_identity_v2.sql` end to end.
2. **Confirm the linked project.** Run `supabase projects list` and check that the project marked as linked is the one you mean to reset. `supabase/.temp/project-ref` holds the linked reference.
3. **Stop traffic.** Stop the deployed backend (or scale it to zero) so nothing writes during the reset. Avoid the ML pipeline's scheduled run at 03:17 UTC; do not start the reset within half an hour of it.
4. **Reset the database** from the backend repository root:

   ```bash
   supabase db reset --linked
   ```

   It asks for confirmation. This is the step that deletes the data.
5. **Check the result** in the Supabase dashboard:
   - Table Editor: the tables exist, `users` has no `phone_number` column, and `pending_registrations` and `login_challenges` exist.
   - `market_locations` has exactly 4 rows.
   - Storage: the `listing-images` bucket still exists (uploaded photos remain as orphans; delete them by hand if you want).
   - Database, Cron Jobs: `expire-stale-orders` and `cleanup-auth-codes` are each listed exactly once.
6. **Run every verification script** in the SQL Editor (each one runs in a transaction that is rolled back and ends with a list of PASS rows): `supabase/verification/schema_checks.sql`, `admin_event_check.sql`, `invites_check.sql`, `pipeline_role_checks.sql` and `identity_v2_check.sql`.
7. **Check the pipeline role.** In the ML repository run the pipeline with `--dry-run` against the project. If the `pipeline_writer` role was lost or its password no longer works, set a new password for it in the SQL Editor (`alter role pipeline_writer with password '...'`), and update the ML repository's `PIPELINE_DATABASE_URL` GitHub secret with the new connection string (see the README).
8. **Configure email for the backend.** Set `EMAIL_PROVIDER` (an HTTPS provider such as `resend` or `brevo` in production; `smtp` only on a host that allows SMTP), `EMAIL_FROM_ADDRESS`, the provider credentials or the `SMTP_*` settings, and `INVITE_CODE_PEPPER` (at least 32 random characters; it now protects every email code). Check `LOGIN_OTP_ROLES` includes `admin` and remove `SEED_ADMIN_PHONE` from the environment (it is no longer read). Run `python scripts/check_config.py` in the target environment.
9. **Seed the main administrator.** Set `SEED_ADMIN_NAME`, `SEED_ADMIN_EMAIL` (and optionally `SEED_ADMIN_PASSWORD`) and run `python -m scripts.seed_admin`. Remove `SEED_ADMIN_PASSWORD` afterwards.
10. **Send the email test.** Start the backend. `POST /api/v1/admin/email/test` needs a signed in super admin, and signing in already needs an emailed code, so for the very first sign in either:
    - run a local backend against the project with `EMAIL_PROVIDER=console` and read the code from `.dev_mailbox/`, or
    - run `ALLOW_ADMIN_RECOVERY=1 python -m scripts.admin_recovery issue-login-code --email <admin email>` and enter the printed challenge token and code with `POST /api/v1/auth/login/verify`.

    Change the initial password when asked, sign in again, then call `POST /api/v1/admin/email/test` from the deployed backend and confirm the email arrives. Unset `ALLOW_ADMIN_RECOVERY`.
11. **Refill prices and forecasts** by running the ML repository's data pipeline and then its forecast pipeline (or wait for the scheduled run). `GET /api/v1/admin/stats` shows `pipeline.series[].forecast_current` once both have run.
12. **Seed demo data if wanted.** `python -m scripts.seed_demo_data` (not in production). Set `DEMO_EMAIL_BASE` to your own address to receive the demo accounts' sign in codes as plus addresses, or use `EMAIL_PROVIDER=console`.
13. **Run the end-to-end check** against a local backend with `EMAIL_PROVIDER=console` and `RATE_LIMITS_DISABLED=1`: `E2E_ADMIN_EMAIL=... E2E_ADMIN_PASSWORD=... python scripts/e2e_check.py`. Every auth step needs the console mailbox; against a deployed API those steps are skipped.
14. **Start traffic again** and tell the frontend team that registration, sign in, invitations and the pay screen changed ([frontend_contract.md](frontend_contract.md)).

## Afterwards

- Former accounts do not exist any more: farmers, buyers and admins register again (admins by a new invitation).
- If email delivery stops, nobody can register or sign in; an operator can still let an administrator in with `scripts/admin_recovery.py` (see [admin.md](admin.md)).
