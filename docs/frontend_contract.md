# Frontend contract

This document is for the frontend repository (the AgriSence PWA and admin screens). It describes how the frontend must call this API. The endpoint reference is [api.md](api.md). The machine-readable contract is [`docs/openapi.json`](openapi.json), and a backend test fails whenever that file differs from the running code.

## Basics

- Base URL: the API origin (for example `https://api.example.com` or `http://localhost:8000` in development) followed by `/api/v1`. Every path below is relative to `/api/v1`.
- Request and response bodies are JSON (`Content-Type: application/json`). The two listing write routes are the only exception: they take `multipart/form-data`.
- Datetimes are ISO 8601 in UTC (for example `2026-10-13T09:05:00Z`); dates are `YYYY-MM-DD`. Convert to the device time zone for display only.
- Money and quantities are JSON numbers rounded to 2 decimals (`45.5`, `112.56`). An order's `total_amount` is a whole number of shillings. Never compute a price or total on the client; show what the server returns.
- Ids are UUID strings. Enums are lowercase strings (`farmer`, `pending_payment`, `one_month`).
- Unknown request fields are rejected with 422. Never send `role`, `status`, prices of orders, totals or ids the server derives.

## Authentication

- `POST /auth/login` with `{"identifier": "<phone or email>", "password": "..."}` returns:

  ```json
  {"access_token": "...", "token_type": "bearer", "expires_in": 3600, "role": "farmer", "is_super_admin": false, "must_change_password": false}
  ```

- `GET /auth/me` returns the profile: `id`, `name`, `phone_number`, `email`, `role`, `is_super_admin`, `must_change_password`, `created_at`. Registration and `POST /auth/accept-invite` return the same shape (with `is_super_admin: false`).

- Send `Authorization: Bearer <access_token>` on every other call. Tokens last 60 minutes (`expires_in` seconds). There is no refresh token: when the token expires, the user logs in again.
- **On any 401, clear the stored session and go to the login screen.** A 401 also happens after the account is disabled or the password was changed on another device.
- Route the user by `role` (`farmer`, `buyer`, `admin`). The super admin is an `admin` account with `is_super_admin: true`. Read the flag from the login response or, after a reload, from `GET /auth/me`; both come from the database row (the token does not carry it). Show the super admin screens (Invites, email check) only when it is true. Do not probe the invite routes to find out: they are rate limited.
- If `must_change_password` is true, or any call returns **403 `password_change_required`**, show the change-password screen. Only `POST /auth/change-password` and `GET /auth/me` work until then. After a successful change, the response says `login_required: true`. Discard the token and log in again.
- Registration: `POST /auth/register/farmer` or `/auth/register/buyer` with `{"name", "phone_number", "email" (optional), "password"}`. The password needs 8 to 128 characters with an upper case letter, a lower case letter, a digit and a symbol. Kenyan phone numbers may be typed as `07…`, `01…`, `+254…` or `254…`.

## Pagination

List routes accept `limit` (1 to 100, default 20) and `offset` (default 0) and return:

```json
{"items": [], "total": 0, "limit": 20, "offset": 0}
```

## Errors

Every error body has this shape (some codes add `reason`, `fields`/`details`, `existing_order_id` or `category`):

```json
{"error": {"code": "order_not_found", "message": "Order not found.", "request_id": "6f1c0e8a-2b44-4c55-9a1e-6c2f8d0b7e11"}}
```

- Branch on `code`, never on `message` (messages may change).
- Every response has an `X-Request-ID` header, and errors repeat it as `request_id`. Show it in error dialogs ("Reference: …") so users can quote it to support. The client may send its own `X-Request-ID` (a UUID) to correlate logs.
- **429** (`rate_limited`, `too_many_payment_attempts`, `too_many_buyer_payment_attempts`, `too_many_phone_numbers`, `resend_cooldown`) and **503** (`service_unavailable`, `busy`) include `Retry-After` in seconds. Disable the action and retry automatically at most once after that many seconds; for 503 `busy` one silent retry is fine.
- **422 `validation_error`** has `fields` (field names) and `details` (`[{field, message}]`); highlight those fields.

Suggested user messages:

| Code | Status | Suggested message |
| --- | --- | --- |
| `invalid_credentials` | 401 | "Wrong phone/email or password." (on other screens: session ended, log in again) |
| `password_change_required` | 403 | "Please set a new password to continue." |
| `forbidden` | 403 | "You don't have access to this page." |
| `wrong_current_password` | 400 | "Your current password is incorrect." |
| `password_too_weak` | 422 | "Choose a stronger password." plus the `reason` |
| `conflict` | 409 | "An account with this phone number or email already exists." |
| `validation_error` | 422 | "Please check the highlighted fields." |
| `body_not_allowed` | 422 | "Something went wrong. Please try again." (client bug) |
| `not_found` / `method_not_allowed` | 404/405 | "This page is not available." (client bug) |
| `payload_too_large` | 413 | "The upload is too large." |
| `rate_limited` | 429 | "Too many attempts. Please wait a moment and try again." |
| `internal_error` | 500 | "Something went wrong on our side. Please try again." plus the reference |
| `service_unavailable` | 503 | "The service is temporarily unavailable. Please try again shortly." |
| `busy` | 503 | "The service is busy. Please try again." |
| `not_implemented` | 501 | "This feature is not available yet." |
| `market_not_available` | 404 | "No price data for this market yet." |
| `forecast_unavailable` | 404 | "Forecast not available yet for this market and crop." |
| `listing_not_found` | 404 | "This listing is no longer available." |
| `listing_limit_reached` | 409 | "You have reached the maximum number of active listings." |
| `listing_removed` | 409 | "This listing was removed and can't be edited." |
| `listing_has_pending_orders` | 409 | "This listing has orders awaiting payment. Try again in a few minutes." |
| `no_changes` | 422 | "Nothing to update." |
| `invalid_image` | 422 | "Use a JPEG or PNG photo." |
| `image_too_large` | 413 | "The photo is too large. Please choose a smaller one." |
| `listing_unavailable` | 409 | "This listing is not available any more." |
| `insufficient_stock` | 409 | "Not enough stock left for this quantity." |
| `order_too_small` | 422 | "The order total is below the minimum." |
| `pending_order_exists` | 409 | "You already have an order awaiting payment for this listing." Offer to open `existing_order_id`. |
| `too_many_pending_orders` | 409 | "Pay or cancel your open orders first." |
| `order_not_found` | 404 | "Order not found." |
| `order_not_cancellable` | 409 | "This order can no longer be cancelled." |
| `order_not_payable` | 409 | "This order can no longer be paid." |
| `order_expiring` | 409 | "This order is about to expire. Please place a new order." |
| `payment_in_progress` | 409 | "A payment is already in progress. Check your phone." |
| `max_attempts_reached` | 409 | "Too many payment attempts for this order. Please place a new order." |
| `idempotency_key_required` / `invalid_idempotency_key` | 422 | "Something went wrong. Please try again." (client bug) |
| `invalid_phone` | 422 | "Enter a valid Safaricom number, for example 0712 345 678." (show it on the number field) |
| `phone_required` | 422 | "Enter the M-Pesa number to send the payment prompt to." (show it on the number field) |
| `idempotency_key_reused` | 409 | "This request was already used with a different number. Start a new payment attempt." Generate a new key and send again. |
| `too_many_payment_attempts` | 429 | "Too many payment attempts for this phone number. Please try again later." |
| `too_many_buyer_payment_attempts` | 429 | "You have made too many payment attempts. Please try again later." |
| `too_many_phone_numbers` | 429 | "You have used too many different phone numbers today. Please try again tomorrow or use one you already used." |
| `payment_request_failed` | 502 | "Could not start the M-Pesa prompt. Please try again." |
| `payments_unavailable` | 503 | "Payments are not available right now." |
| `invalid_or_expired_code` | 400 | "The code is invalid or has expired." |
| `codes_unavailable` | 503 | "This feature is temporarily unavailable." |
| `user_exists` | 409 | "An account with this phone number or email already exists." |
| `too_many_pending_invites` | 409 | "Too many open invitations. Revoke one first." |
| `invite_conflict` | 409 | "An invitation for this person was just created. Refresh the list." |
| `invite_not_found` | 404 | "Invitation not found." |
| `invite_not_pending` | 409 | "This invitation is no longer open." |
| `send_limit_reached` | 409 | "No more codes can be sent for this invitation." |
| `resend_cooldown` | 429 | "Please wait a minute before sending another code." |
| `no_email` | 409 | "Your account has no email address." |
| `email_failed` | 502 | "The email could not be sent." plus the `category` |
| `user_not_found` | 404 | "User not found." |
| `cannot_disable_self` | 403 | "You can't disable your own account." |
| `cannot_modify_super_admin` | 403 | "The super administrator can't be changed." |
| `order_not_in_reconciliation` | 409 | "This order no longer needs review." |
| `reconciliation_context_missing` | 409 | "This order can't be resolved automatically; check the payment events." |
| `insufficient_stock_to_confirm` | 409 | "Not enough stock is left to confirm this sale." |
| `listing_has_open_payments` | 409 | "A buyer is paying for this listing right now. Try again in a few minutes." |
| `payment_not_found` | 404 | "Payment not found." |

Any other code: show the generic message plus the reference.

## Screens and endpoints

| Screen | Roles | Endpoints |
| --- | --- | --- |
| Register | public | `POST /auth/register/farmer`, `POST /auth/register/buyer` |
| Login | public | `POST /auth/login`, then `GET /auth/me` |
| Change password | all signed-in users | `POST /auth/change-password` |
| Forgot password, reset password | public | `POST /auth/password-reset/request`, `POST /auth/password-reset/confirm` |
| Accept admin invitation | public | `POST /auth/accept-invite`, `POST /auth/invite/request-code` |
| Market prices | farmer, admin | `GET /markets`, `GET /prices/current?crop=`, `GET /prices/trends?market=&crop=&months=` |
| Forecast | farmer, admin | `GET /forecasts?market=&crop=&horizon=` |
| My listings, new listing | farmer | `GET /listings/mine`, `POST /listings`, `DELETE /listings/{id}` |
| Edit listing | farmer | `GET /listings/mine/{id}` to load it (any status; a `removed` listing is shown read-only), then `PATCH /listings/{id}` |
| Orders on my listings | farmer | `GET /listings/mine/orders` |
| Browse produce, listing detail | buyer | `GET /listings`, `GET /listings/{id}`; the market filter reads `GET /listings/markets` |
| Checkout and order status | buyer | `POST /orders`, `GET /orders/{id}`, `POST /orders/{id}/pay`, `POST /orders/{id}/cancel` |
| My orders | buyer | `GET /orders/mine` |
| Admin dashboard | admin | `GET /admin/stats` |
| Users | admin | `GET /admin/users`, `PATCH /admin/users/{id}/disable`, `PATCH /admin/users/{id}/enable` |
| Listing moderation | admin | `GET /admin/listings`, `DELETE /admin/listings/{id}` |
| Orders and payments, reconciliation | admin | `GET /admin/orders`, `GET /admin/orders/{id}`, `POST /admin/orders/{id}/resolve`, `GET /admin/payments`, `GET /admin/payments/{id}/events`, `GET /orders/{id}` (read only) |
| Forecast accuracy | admin | `GET /admin/forecast-accuracy` |
| Admin invitations (Invites), email check | super admin only: shown when `is_super_admin` is true in the profile | `POST /admin/invites`, `GET /admin/invites`, `POST /admin/invites/{id}/resend`, `DELETE /admin/invites/{id}`, `POST /admin/email/test` |

Buyers never see prices or forecasts (403). Farmers never see the buyer browse screens (403). Hide the super admin screens unless the profile has `is_super_admin: true`. The server still checks every role, so hiding a screen is only a convenience.

- Buyer market filter: `GET /listings/markets` returns `[{"name": "Kisumu", "listing_count": 2}, ...]` ordered by name, only for markets with at least one listing the buyer can buy (`[]` when nothing is listed). Offer exactly these names and pass the chosen one as `GET /listings?market=<name>`; its `total` equals that `listing_count`. It carries no prices.
- Farmer edit screen: `GET /listings/mine/{id}` returns the same `ListingOut` as a `GET /listings/mine` item. A 404 `listing_not_found` means the listing does not exist or is not the farmer's; go back to My listings.

### Forecast display

- Headline: `predicted_price` (KES per kg) and `expected_change_pct` with a sign and an arrow (for example "+2.5%"). Compare with `origin_price`, which is the latest observed price.
- Caption: "Based on <`based_on` as month and year>, for <`for_month` as month and year>", for example "Based on September 2026, for October 2026".
- Explanation bars come from `shap_values`, which the API passes through unchanged from the ML pipeline:
  - one "typical change" bar for the model's baseline (the expected change before the drivers are considered);
  - the three drivers with the largest absolute contribution, each as a bar pointing up (pushes the price up) or down, labelled with a readable name.
  - Confirm the exact keys of `shap_values` with the ML repository (`ucs35-mudibo-ml`) and read them defensively. If a key is missing, hide the bars and keep the headline.
- Always show a short note that the bars are approximate: they explain the model's estimate, they do not guarantee prices, and they may not add up exactly.
- **404 `forecast_unavailable`** (reason `no_forecast` or `stale`): show "Forecast not available yet for this market and crop." Do not show an older forecast.

## Checkout and payment

1. `POST /orders` with `{"listing_id": "...", "quantity": "2.5"}` reserves stock and returns the order (`OrderOut`). Show `total_amount` from the response.
2. Countdown: compute the remaining time as `expires_at - server_time` from the response, then count down locally. Never use the device clock alone, because it may be wrong.
3. Pay: ask for the M-Pesa number on the pay screen, then `POST /orders/{id}/pay` with `{"phone_number": "<as typed>"}` and header `Idempotency-Key: <UUID>`. Accept `07…`, `01…`, `+254…` or `254…`, with spaces or dashes; the server normalises it. Generate **one new UUID per click** on "Pay". If that request fails at the network level or times out, retry with the **same** key and the **same** number. A replay returns the same 202 and never sends a second prompt; a replay with a different number gives 409 `idempotency_key_reused`, so a changed number always needs a new key. The 202 response has `message` ("Check your phone and enter your M-Pesa PIN.") and `prompt` (the masked phone the prompt went to). Show both.
   - The body is optional for now: without it (or with `{}`) the profile phone number is used while accounts still have one. Once phone numbers are removed from accounts the field becomes effectively required (422 `phone_required` without it), so always send it.
   - The number is used for that payment only and is not saved to the profile. Prefilling the field from the last number used on this device is fine.
   - `payment.phone_masked` is the entered number and `payment.stk_target_masked` the number the prompt went to (the sandbox test number on a test server), both masked to the last 3 digits. Either is `null`/absent only when there is no payment (`payment` is `null`).
   - 422 `invalid_phone` or `phone_required` belong on the number field. The three payment 429 codes carry `Retry-After`; disable Pay for that long.
4. Poll `GET /orders/{id}` **every 3 seconds for up to 150 seconds**. Stop as soon as the order status is final, or the latest payment status is final while the order is still `pending_payment`.
5. Show the `note` from the order response. Payments run in the Safaricom sandbox, a paid order is a simulated confirmation, and delivery is arranged outside the platform.

Buttons: show **Pay** only when `can_pay` is true and **Cancel** only when `can_cancel` is true. Both already account for ownership, the expiry margin, open payments and the attempt limit, so do not reimplement those rules.

| Order status | Meaning | Screen |
| --- | --- | --- |
| `pending_payment` | Stock reserved and waiting for payment. `is_expired` is true once `expires_at` has passed. | Countdown, Pay and Cancel per `can_pay` / `can_cancel`. When `is_expired`, show "expired" and offer a new order. |
| `paid` | Payment confirmed (simulated sandbox confirmation). | Success with the receipt (`payment.mpesa_receipt_number`). Final. |
| `payment_failed` | The payment did not complete and the order is closed. | Failure, with an offer to order again. Final. |
| `expired` | Not paid in time; the stock was returned. | "This order expired." Final. |
| `cancelled` | Cancelled by the buyer or an admin; stock returned. | "Order cancelled." Final. |
| `needs_reconciliation` | A payment arrived in an unexpected state, so an admin will review it. | "We're checking your payment; no action needed." Final for polling. |

| Payment status (`payment.status`) | `payment.status_message` | Screen |
| --- | --- | --- |
| `initiating` | Sending the payment request | Spinner, keep polling |
| `initiated` | Waiting for you to confirm on your phone | "Check your phone", keep polling |
| `completed` | Payment received | Keep polling until the order is `paid` (usually the same response) |
| `cancelled` | You cancelled the payment request | Stop polling; Pay again if `can_pay` |
| `timeout` | The request timed out | Stop polling; Pay again if `can_pay` |
| `failed` | The payment failed | Stop polling; Pay again if `can_pay` |

If 150 seconds pass with no final status, stop polling and show "We haven't received confirmation yet" with a manual refresh button.

## Uploads

- `POST /listings` (multipart) fields: `crop` (`maize` or `beans`), `market`, `quantity_available`, `unit` (`kg`, optional), `price_per_unit`, and optional `image`.
- `PATCH /listings/{id}` (multipart) fields, all optional: `quantity_available`, `price_per_unit`, `image` (replaces the photo), `remove_image` (`true`). Any other field is rejected.
- `image` must be JPEG or PNG and at most `MAX_IMAGE_BYTES` (5 MiB by default). A larger photo gets 413 `image_too_large`, and a whole request above that limit plus 64 KiB gets 413 `payload_too_large`. **Compress and resize on the client before uploading** (for example to 1600 px on the longest side, JPEG quality around 0.8). The server re-encodes every image as JPEG without metadata, so the client does not need to strip it.
- The response's `image_url` is a full URL to show directly (or `null`). Do not build storage URLs yourself.

## Public account pages

These pages need no token.

- Accept invitation (link in the invite email, `FRONTEND_BASE_URL/accept-invite`): `POST /auth/accept-invite` with `{"email": "...", "code": "...", "password": "..."}`. Admin passwords need at least 12 characters. Success is 201; then send the user to login. To get a new code: `POST /auth/invite/request-code` with `{"email": "..."}` (always 202).
- Forgot password: `POST /auth/password-reset/request` with `{"email": "..."}` (always 202 with the same message, whether or not the account exists).
- Reset password (`FRONTEND_BASE_URL/reset-password`): `POST /auth/password-reset/confirm` with `{"email": "...", "code": "...", "new_password": "..."}`. Success is 204; then go to login.
- Code input: codes are 8 characters shown as `XXXX-XXXX`. Accept lower case, spaces and hyphens, and send what the user typed. The server normalises it. Never put a code in a URL.
- Every failure returns the same 400 `invalid_or_expired_code`. Show one generic message and do not suggest which part was wrong.

## CORS

- The browser may call the API only from the origins listed in the server's `CORS_ORIGINS` setting (exact `scheme://host[:port]`, no wildcard in production). Ask the backend team to add the frontend's development and production origins.
- Allowed methods: `GET, POST, PATCH, DELETE, OPTIONS`. Allowed request headers: `Authorization`, `Content-Type`, `Idempotency-Key`, `X-Request-ID`. The browser can read the `X-Request-ID` and `Retry-After` response headers.
- Credentials (cookies) are not used and not allowed. Keep the token in memory or in storage scoped to the app, and send it as a bearer header.

## TypeScript types

`docs/openapi.json` is the source of truth for request and response shapes. A backend test fails when the file differs from the code, so the committed file always matches the API. Generate types from it in the frontend repository, for example:

```bash
npx openapi-typescript ../ucs35-mudibo-backend/docs/openapi.json -o src/api/schema.d.ts
```

Regenerate the types whenever the backend changes `docs/openapi.json`. The Daraja callback route is deliberately not in the schema.
