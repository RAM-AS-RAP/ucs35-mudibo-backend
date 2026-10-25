# Authentication decisions

- Registration roles are chosen by the route and persisted database record, never by the client.
- Login failures use the same generic 401 response for unknown users, incorrect passwords, and disabled accounts.
- Access tokens are stateless, while each request reloads the account and role from the database.
- Identity v2 replaced the earlier "OTP is out of scope" decision: see the identity v2 decisions below.
- Per email login throttling is in-process and resets when the application process restarts.
- Record the FEWS API probe result here, then delete `.github/workflows/fews-probe.yml`.
- FEWS API returns 200 from GitHub Actions runners (probe run 9 Oct 2026). The monthly pipeline fetches FEWS directly.
- The ML pipeline connects as `pipeline_writer` with select on `market_locations`, select/insert and column-limited update on `market_prices` and `forecasts`, and no other access; the backend never uses this role.
- Displayed money values are serialized as JSON numbers rounded to two decimals; calculations remain `Decimal`.
- Listing units are limited to `kg` until additional units are explicitly supported.
- Uploaded images are re-encoded as JPEG after EXIF orientation is applied, with metadata removed.
- Missing listings and listings owned by another farmer return the same 404 response.
- Forecasts with stale origins are hidden rather than shown against old price data.
- Farmer order history omits buyer contact details and exposes only the buyer's name.
- `quantity_available` means stock currently available after order reservations have been deducted.
- Order totals are computed on the server as `ceil(quantity × unit_price)` in `Decimal` (whole shillings); clients never send a price or total.
- Order `expires_at` is set from the database clock (`now()` + `ORDER_EXPIRY_MINUTES`), never the application clock.
- Lock order: wherever an order and a listing are both locked, the order is locked first (matching `expire_stale_orders()`); checkout locks only the listing before inserting the new order.
- A second pending order by the same buyer for the same listing is rejected with `pending_order_exists` (returning the existing order id), not merged.
- Orders require at least `MIN_ORDER_QUANTITY` (default 1 kg) and a total of at least `MIN_ORDER_TOTAL_KES` (default 10).
- Buyers see the seller's name on listings and orders, but never the farmer's id, phone number, or email.
- Only a `pending_payment` order without an open payment can be cancelled; cancelling returns stock and is idempotent. Module 5's pay endpoint must lock the order row `FOR UPDATE` before inserting a payment and check that it is `pending_payment` and not expired, rather than relying on the payment insert trigger.
- Admins may read a single order (`GET /orders/{id}`) but cannot act on it; `can_pay` and `can_cancel` are always false for them.
- The super admin flag is exposed to the signed-in user (login response and profile) from the database row, never from the token; it is a UI hint only and authorization still reads the row.
- Farmers can read one of their own listings (`GET /listings/mine/{id}`) in any status, including removed ones; another farmer's listing is the same 404 as an unknown id.
- Buyers see only markets that currently have buyer-visible listings (`GET /listings/markets`), not markets with price data, and no price data at all.
- One shared visibility rule for buyer listings (active, in stock, farmer not disabled: `buyer_visible_listings_clause()` and its loaded-row form) decides browse, the markets list, the detail `available` flag and checkout.

# Payment decisions

- A payment row (`initiating`) is created before the Daraja call, and the checkout and merchant request ids are saved afterwards.
- No database transaction is held open during an HTTP call to Daraja; STK Push is never retried automatically.
- Callbacks always return 200 after a valid token and are recorded in the append-only `payment_events` log first; a callback that arrives before its checkout id is saved is kept as an orphan event and matched later.
- The phone number in a callback is masked by Safaricom and is never used for any lookup.
- An STK Push Query result has no receipt and no amount, so a payment completed by a query keeps a null receipt and skips the amount check.
- A late payment on an expired or cancelled order makes the order `needs_reconciliation`, never `paid`, because its stock was already restored.
- At least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` (90) seconds must remain before expiry to start a payment.
- Payment attempts are limited to `MPESA_MAX_ATTEMPTS_PER_ORDER` (3) per order and `MPESA_PHONE_ATTEMPTS_PER_HOUR` (5) per entered number per hour.
- The M-Pesa number is entered per payment (`POST /orders/{id}/pay` body, required since identity v2) and stored only on the payment; there is no profile number to fall back to.
- The per number limit counts the entered number (`payments.phone_number`), not `stk_phone_number`, which fixes the shared test number being exhausted for every buyer in `test_msisdn` mode.
- Per buyer (`MPESA_BUYER_ATTEMPTS_PER_HOUR`, 10) and distinct number (`MPESA_BUYER_DISTINCT_PHONES_PER_DAY`, 3) limits apply because entered numbers are unverified; a per buyer advisory lock taken before the order lock makes them exact across the buyer's orders.
- `MPESA_STK_TARGET=registered` is a deprecated alias of `entered` (startup warning); `entered` is preferred.
- A replay of an `Idempotency-Key` with a different number is rejected with 409 `idempotency_key_reused`.
- The uvicorn access log rewrites `/payments/callback/<token>` to `/payments/callback/***`, so the callback token is never logged.
- The `payment_failed` order status stays unused; a failed payment leaves the order `pending_payment` until it expires.
- Production payments are impossible by construction: settings and the Daraja client reject any non-sandbox environment, URL or shortcode, and there is no production option.
- When a payment result sets an order to `needs_reconciliation`, an `error` event with the reason and `order_status_before` is written in the same transaction; it is the context admin resolution relies on.

# Admin decisions

- No credential is ever returned by an API: new admins are invited by an emailed code and choose their own password, and forgotten passwords are reset by an emailed code.
- Codes are 8 characters without look-alike letters; only an HMAC (keyed with `INVITE_CODE_PEPPER`, bound to the purpose and record id) is stored, with a total attempt cap per invite (10) and per reset code (5).
- Every failed invite acceptance or reset confirmation returns the same 400 `invalid_or_expired_code`, and the public code request routes always return the same 202.
- Resending an invite code never resets its attempt counter.
- Password changes (change-password, invite acceptance, reset, seed) set `password_changed_at` and revoke every access token issued before that second.
- Every account has a verified email since identity v2, so every user can reset a forgotten password by email.
- Invite emails are sent synchronously after the commit, so the super admin sees `email_sent`; public code emails are sent in background tasks so response times do not reveal whether an account or invite exists.
- Email goes through HTTPS APIs (Resend, Brevo) or SMTP with verified TLS (for example Gmail); hosts such as Render's free tier block SMTP ports, and sends are never retried automatically.
- The super admin cannot be disabled, demoted or deleted.
- Only the super admin invites, disables or enables other admins; ordinary admins manage farmers and buyers.
- Admin listing removal cancels the listing's unpaid orders and returns their stock, and is refused while one of those orders has an open payment.
- Disabled farmers' listings are hidden from buyers (browse, detail and checkout), not deleted; enabling the farmer restores them.
- Resolutions of `needs_reconciliation` orders are recorded as `admin_action` payment events; refunds are handled outside the platform.
- Forecast accuracy is reported separately for backfilled and live forecasts, and pending forecasts are excluded from the metrics.

# Hardening decisions (Module 7)

- Request ids: every request gets a UUID in `X-Request-ID` (a client-supplied id is reused only when it is a valid UUID), and the same id is in every error body and every log line, so a user can quote it to support.
- Body limits: JSON and form bodies are capped at `MAX_JSON_BODY_BYTES` (64 KiB) and the two multipart listing routes at `MAX_IMAGE_BYTES` + 64 KiB, checked from `Content-Length` and again while streaming; the Daraja callback keeps its own `CALLBACK_MAX_BYTES` rule.
- Statement and lock timeouts are applied with `SET LOCAL` at the start of every transaction (engine `begin` event), because the Supabase transaction pooler does not accept startup parameters; setting both to 0 turns the feature off.
- Error mapping: database connection failures and pool timeouts return 503 `service_unavailable` (`Retry-After: 5`), lock timeouts, deadlocks and serialization failures return 503 `busy` (`Retry-After: 1`), and every other unexpected error returns a generic 500 with only the request id; driver text and SQL never reach a response.
- Every response is `Cache-Control: no-store`: the API serves personal and fast-changing data, and nothing is safe to cache in a shared or browser cache.
- `tests/route_matrix.py` is the single authorization inventory: a test fails when a route is missing from it, and every role outside a route's set must get 401 or 403 before validation or database access.
- `docs/openapi.json` is the frontend contract: the frontend generates its types from it, and a test fails when it differs from the application.
- `Retry-After` on 429 is computed from slowapi's window statistics in the exception handler, because slowapi 0.1.10's `headers_enabled=True` raises for routes that return a model and take no `response` parameter.
- CORS sits outside the host check, the body limit and the generic 500 handler, so the browser can read those error bodies; credentials are never allowed because the API uses bearer tokens, not cookies.

# Identity v2 decisions

- Accounts are identified only by a verified, lower case email; phone numbers are removed from accounts and invitations and exist only on payments.
- The data is wiped before the identity v2 migration (`supabase db reset --linked`), and the migration refuses to run on a database with users or invites, so no phone or email conversion is needed.
- Farmers and buyers confirm their email with a code before the account exists (`pending_registrations`); the registration code counts as the second step, so verification returns a signed in result.
- Every password sign in for the roles in `LOGIN_OTP_ROLES` needs an emailed code bound to a challenge token; production refuses a list without `admin`.
- A sign in code is sent only after a correct password, synchronously after the commit, so the client learns `code_sent`; registration emails go out in background tasks so timing reveals nothing.
- Registration always hashes the password and answers the same 202 shape; an existing address gets a notice email (at most once per `REG_EXISTS_NOTICE_MINUTES`, per process) and a token that is stored nowhere.
- Submitting a registration again rotates the token and keeps a fresh code; `role` is immutable on a pending row (database trigger), so a registration for the other role replaces the row.
- Locked or expired pending registrations are closed (marked used) when the address registers again, instead of being reopened.
- Wrong codes count across resends for registrations and login challenges; a resend never resets the counter, and the counter is committed before the comparison.
- One active login challenge per user (partial unique index); a new sign in supersedes the old challenge, serialised by a transaction scoped advisory lock.
- Codes are HMACs keyed by `INVITE_CODE_PEPPER` (name kept for compatibility) with the purposes `invite`, `reset`, `register` and `login`; tokens are 256 bit `secrets.token_urlsafe(32)` stored as SHA-256.
- `email` and `email_verified_at` never change once written (database trigger); there is no email change feature.
- Invitations take name and email only, and accepting one returns a signed in result because the invite code proves control of the email.
- `ACCESS_TOKEN_EXPIRE_MINUTES` defaults to 480 (8 hours) because every sign in now needs an emailed code.
- `PayRequest.phone_number` is required; a missing body, field or value is 422 `phone_required`, decided by the service so the code is stable.
- `scripts/admin_recovery.py` (behind `ALLOW_ADMIN_RECOVERY=1`) lets an operator with database access reset an admin password or issue a sign in code when email is down.
- Old registrations, login challenges and reset codes are deleted after 2 days by the `cleanup-auth-codes` pg_cron job; invites are kept as history.
