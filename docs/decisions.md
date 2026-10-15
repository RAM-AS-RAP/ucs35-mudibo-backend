# Authentication decisions

- Registration roles are chosen by the route and persisted database record, never by the client.
- Login failures use the same generic 401 response for unknown users, incorrect passwords, and disabled accounts.
- Access tokens are stateless, while each request reloads the account and role from the database.
- OTP is out of scope; phone verification is a documented limitation of this module.
- Identifier login throttling is in-process and resets when the application process restarts.
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

# Payment decisions

- A payment row (`initiating`) is created before the Daraja call, and the checkout and merchant request ids are saved afterwards.
- No database transaction is held open during an HTTP call to Daraja; STK Push is never retried automatically.
- Callbacks always return 200 after a valid token and are recorded in the append-only `payment_events` log first; a callback that arrives before its checkout id is saved is kept as an orphan event and matched later.
- The phone number in a callback is masked by Safaricom and is never used for any lookup.
- An STK Push Query result has no receipt and no amount, so a payment completed by a query keeps a null receipt and skips the amount check.
- A late payment on an expired or cancelled order makes the order `needs_reconciliation`, never `paid`, because its stock was already restored.
- At least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` (90) seconds must remain before expiry to start a payment.
- Payment attempts are limited to `MPESA_MAX_ATTEMPTS_PER_ORDER` (3) per order and `MPESA_PHONE_ATTEMPTS_PER_HOUR` (5) per prompt phone per hour.
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
- Farmers and buyers without an email address cannot reset a forgotten password (documented limitation).
- Invite emails are sent synchronously after the commit, so the super admin sees `email_sent`; public code emails are sent in background tasks so response times do not reveal whether an account or invite exists.
- Email goes through HTTPS APIs (Resend, Brevo) or SMTP with verified TLS (for example Gmail); hosts such as Render's free tier block SMTP ports, and sends are never retried automatically.
- The super admin cannot be disabled, demoted or deleted.
- Only the super admin invites, disables or enables other admins; ordinary admins manage farmers and buyers.
- Admin listing removal cancels the listing's unpaid orders and returns their stock, and is refused while one of those orders has an open payment.
- Disabled farmers' listings are hidden from buyers (browse, detail and checkout), not deleted; enabling the farmer restores them.
- Resolutions of `needs_reconciliation` orders are recorded as `admin_action` payment events; refunds are handled outside the platform.
- Forecast accuracy is reported separately for backfilled and live forecasts, and pending forecasts are excluded from the metrics.
