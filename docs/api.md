# API endpoints

All paths are prefixed with `/api/v1`. Authenticated routes require a bearer access token. `farmer`, `buyer`, and `admin` describe allowed roles; super-admin routes require an admin account with the super-admin flag. The authoritative list of routes and allowed roles is `tests/route_matrix.py`. A test checks that every route has exactly one `### METHOD /api/v1/path` heading below. The machine-readable contract for the frontend is `docs/openapi.json` (see [frontend_contract.md](frontend_contract.md)).

Every response carries `X-Request-ID` (a UUID; a valid UUID sent by the client is reused) and `Cache-Control: no-store`. Every error body is `{"error": {"code", "message", "request_id"}}`, sometimes with `reason` or other documented fields. These errors can come from any route: 401 `invalid_credentials`, 403 `forbidden` or `password_change_required`, 404 `not_found` (unknown route), 405 `method_not_allowed`, 413 `payload_too_large` (JSON or form bodies above `MAX_JSON_BODY_BYTES`, multipart listing bodies above `MAX_IMAGE_BYTES` + 64 KiB), 422 `validation_error`, 429 `rate_limited` (with `Retry-After`), 500 `internal_error` (generic message only), 503 `service_unavailable` (`Retry-After: 5`, database unreachable) or `busy` (`Retry-After: 1`, lock timeout, deadlock or serialization failure).

## Endpoint reference

### GET /api/v1/health

Public. No database access. Returns `{"status": "ok"}`.

### GET /api/v1/health/db

Public. Runs `select 1`. Returns `{"status": "ok"}`, or 503 `{"status": "degraded"}` without details.

### POST /api/v1/auth/register/farmer

Public, 10 per hour per IP. JSON `name`, `email`, `password`; unknown fields such as `role` or `phone_number` are rejected with 422. Always returns 202 `RegistrationAccepted` with the same shape whether or not the address already has an account; the account is created only by `POST /auth/register/verify`. See [Registration](#registration).

### POST /api/v1/auth/register/buyer

Public, 10 per hour per IP. Same body and response as farmer registration; the role comes from the route.

### POST /api/v1/auth/register/verify

Public, 5 per minute per IP. JSON `registration_token`, `code`. Creates the account and returns 201 `AuthResult` with `status: "signed_in"`; every failure is the same 400 `invalid_or_expired_code`.

### POST /api/v1/auth/register/resend

Public, 5 per hour per IP. JSON `registration_token`. Always returns 202 `{message, resend_available_in}`; a new code is emailed only when the registration allows it.

### POST /api/v1/auth/login

Public, 5 per minute per IP. JSON `email`, `password`. Returns 200 `AuthResult`: `status: "code_required"` with a `challenge_token` for the roles in `LOGIN_OTP_ROLES` (an emailed code follows), or `status: "signed_in"` with the access token. See [Sign in](#sign-in).

### POST /api/v1/auth/login/verify

Public, 10 per minute per IP. JSON `challenge_token`, `code`. Returns 200 `AuthResult` with `status: "signed_in"`; 400 `invalid_or_expired_code` for a wrong code, 400 `challenge_closed` when the challenge is unknown, used, locked or expired.

### POST /api/v1/auth/login/resend

Public, 5 per hour per IP. JSON `challenge_token`. Returns 200 `{code_sent, resend_available_in, code_expires_at}`; 429 `resend_cooldown`, 409 `send_limit_reached`, 400 `challenge_closed`.

### POST /api/v1/auth/change-password

Any authenticated user, also while a password change is required. Current and new password. Returns 200 with a note that the user must log in again; earlier tokens are revoked.

### GET /api/v1/auth/me

Any authenticated user. Returns the current account (`UserOut`), including `is_super_admin` read from the database row.

### POST /api/v1/auth/accept-invite

Public. JSON `email`, `code`, `password`. Returns 201 `AuthResult` with `status: "signed_in"` for the new admin (the invite code proves control of the email, so no sign in code follows). See [Invites and password reset](#invites-and-password-reset).

### POST /api/v1/auth/invite/request-code

Public. JSON `email`. Always returns 202 with the same body.

### POST /api/v1/auth/password-reset/request

Public. JSON `email`. Always returns 202 with the same body.

### POST /api/v1/auth/password-reset/confirm

Public. JSON `email`, `code`, `new_password`. Returns 204.

### GET /api/v1/markets

Farmer, admin. Markets with price data, with each market's latest observed month.

### GET /api/v1/prices/current

Farmer, admin. `crop=maize` or `crop=beans`. Returns `{items, as_of_note}`. Each item has the current price, source and fetched time, plus the previous calendar month's price and change (null when missing). `items` is empty if the crop has no data.

### GET /api/v1/prices/trends

Farmer, admin. Available `market`, `crop`, `months` (1-60, default 24). Returns the last `months` calendar months in ascending order; missing months are absent. 404 `market_not_available` if no price data exists for that market.

### GET /api/v1/forecasts

Farmer, admin. Available `market`, `crop`, `horizon=one_month` or `two_month`. Returns the stored forecast with `expected_change_pct`, unchanged SHAP values, `based_on`, and `for_month`; 404 if absent or stale.

### POST /api/v1/listings

Farmer. Multipart crop, market, quantity, `unit=kg`, price, optional JPEG/PNG image. Returns the created `ListingOut` (201). Errors: `market_not_available` (404), `listing_limit_reached` (409), `invalid_image` (422), `image_too_large` (413).

### GET /api/v1/listings/mine

Farmer. Optional `status`, `limit` (1-100, default 20), `offset`. Own listings, newest first; removed listings are included by default.

### GET /api/v1/listings/mine/orders

Farmer. Optional order `status`, `listing_id`, `limit` (1-100, default 20), `offset`. Paginated orders on the farmer's own listings, with latest payment metadata, the buyer's name only, and a sandbox/delivery note.

### GET /api/v1/listings/mine/{listing_id}

Farmer. Path `listing_id` (UUID). Returns one of the farmer's own listings as `ListingOut` (the same fields as a `GET /listings/mine` item) in any status, including `sold_out` and `removed`. No buyer or order data. Another farmer's listing and an unknown id return the same 404 `listing_not_found`; a non-UUID id returns 422. See [Details: `GET /listings/mine/{id}`](#details-get-listingsmineid).

### PATCH /api/v1/listings/{listing_id}

Owning farmer. Multipart optional quantity, price, image replacement, or `remove_image`. Returns the updated `ListingOut`. Errors: `listing_not_found` (404), `listing_removed` (409), `no_changes` (422).

### DELETE /api/v1/listings/{listing_id}

Owning farmer. Soft-removes the listing and returns 204; a repeat delete is idempotent. `listing_has_pending_orders` returns 409.

### GET /api/v1/listings

Buyer. Optional `crop`, `market`, `min_price`, `max_price`, `sort`, `limit`, `offset`. Paginated active listings with stock (`PublicListingOut`). See [Buyer marketplace API](#buyer-marketplace-api).

### GET /api/v1/listings/markets

Buyer. No parameters. Markets that have at least one listing a buyer could buy (same rule as `GET /listings`), ordered by name, as `[{"name", "listing_count"}]`; `[]` when nothing is listed. No price data. See [Details: `GET /listings/markets`](#details-get-listingsmarkets).

### GET /api/v1/listings/{listing_id}

Buyer. An active or sold-out listing with `available`; a removed or unknown listing is 404.

### POST /api/v1/orders

Buyer. JSON `listing_id` and `quantity`. Returns the created `OrderOut` (201) with stock reserved and a server-calculated total.

### GET /api/v1/orders/mine

Buyer. Optional `status`, `limit`, `offset`. Own orders, newest first, with listing and payment summaries.

### GET /api/v1/orders/{order_id}

Owning buyer, or admin (read only). Returns `OrderOut`.

### POST /api/v1/orders/{order_id}/cancel

Owning buyer. Returns the cancelled `OrderOut`; stock is returned to the listing.

### POST /api/v1/orders/{order_id}/pay

Owning buyer. `Idempotency-Key` header; required body `{"phone_number": "..."}` (the M-Pesa number for this payment; a missing body, field or value is 422 `phone_required`). Returns 202 `OrderOut` plus `message` and `prompt`; an M-Pesa sandbox STK prompt is sent.

### POST /api/v1/payments/callback/{token}

Daraja only. There is no bearer authentication: the secret path token is the check. Body: Daraja STK callback JSON. A wrong token gets 404 with an empty body; otherwise the response is always 200 `{"ResultCode": 0, "ResultDesc": "Accepted"}`. This route has its own body limit (`CALLBACK_MAX_BYTES`, 413 with an empty body) and is not in the OpenAPI schema.

### POST /api/v1/admin/invites

Super admin. JSON `name`, `email` (a `phone_number` field is rejected with 422). Returns a 201 invite summary; the code is emailed, never returned.

### POST /api/v1/admin/invites/{invite_id}/resend

Super admin. Empty body. Returns the invite summary; a new code is emailed.

### GET /api/v1/admin/invites

Super admin. Optional `status`, `limit`, `offset`. Paginated invites.

### DELETE /api/v1/admin/invites/{invite_id}

Super admin. Revokes a pending invite and returns 204.

### POST /api/v1/admin/email/test

Super admin. Empty body. Returns `{"email_sent": true}`, or 502 `email_failed` with a `category`.

### GET /api/v1/admin/users

Admin. Optional `role`, `is_disabled`, `q` (name or email), `limit`, `offset`. Paginated `UserAdminOut`, newest first.

### PATCH /api/v1/admin/users/{user_id}/disable

Admin (super admin for admin targets). Empty body. Returns `UserAdminOut`; idempotent.

### PATCH /api/v1/admin/users/{user_id}/enable

Admin (super admin for admin targets). Empty body. Returns `UserAdminOut`; idempotent.

### GET /api/v1/admin/listings

Admin. Optional `status`, `crop`, `market`, `farmer_id`, `q`, `limit`, `offset`. Paginated `AdminListingOut`.

### DELETE /api/v1/admin/listings/{listing_id}

Admin. Soft removal that cancels unpaid orders; returns 204.

### GET /api/v1/admin/orders

Admin. Optional `status`, `q`, `limit`, `offset`. Paginated `AdminOrderOut`.

### GET /api/v1/admin/orders/{order_id}

Admin. `AdminOrderOut` with `payments` and `resolution`.

### POST /api/v1/admin/orders/{order_id}/resolve

Admin. JSON `action`, `note`. Returns the resolved order.

### GET /api/v1/admin/payments

Admin. Optional `status`, `order_reference`, `environment`, `created_from`, `created_to`, `limit`, `offset`. Paginated `AdminPaymentOut`.

### GET /api/v1/admin/payments/{payment_id}/events

Admin. Event timeline, oldest first, at most 100.

### GET /api/v1/admin/stats

Admin. `StatsOut` dashboard counts and pipeline status.

### GET /api/v1/admin/forecast-accuracy

Admin. Optional `market`, `crop`, `horizon`, `is_backfill`, `include_pending`, `limit`, `offset`. `AccuracyOut` rows and summary.

### GET /docs

Public, development only (disabled when `APP_ENV=production`). Swagger UI. The docs pages do not get the strict `Content-Security-Policy`.

### GET /docs/oauth2-redirect

Public, development only. Swagger UI helper page.

### GET /redoc

Public, development only. ReDoc reference.

### GET /openapi.json

Public, development only. The live OpenAPI schema; the committed copy is `docs/openapi.json`.

## Overview

Error examples in the rest of this document leave out `request_id` for brevity; real error bodies always include it.

Admin routes are described in [Admin API](#admin-api) and operated with [admin.md](admin.md). Payments run only against the Safaricom Daraja sandbox (see [payments.md](payments.md)). Buyers cannot see prices or forecasts and cannot access `GET /markets` or farmer listing routes; only farmers and admins can read market data. Buyers get `GET /listings/markets` instead, which lists only markets with buyer-visible listings and their counts.

Only markets with published price data are offered (currently Nairobi and Kisumu). Crop values are `maize` or `beans`; forecast horizons are `one_month` or `two_month`. Market/crop values are normalized for case and surrounding whitespace. Unsupported values return 422; no-data markets return 404 `market_not_available`. A forecast is shown only when its origin equals the series' latest price month and that month is no more than two calendar months old in `Africa/Nairobi`; otherwise it returns 404 `forecast_unavailable` with reason `no_forecast` or `stale`. Prices and forecasts are read from stored pipeline output; this API does not invent or compute them.

## Farmer API statuses and errors

All protected routes return `401` when unauthenticated and `403 forbidden` when the authenticated role is not allowed. Successful statuses and endpoint-specific errors are:

| Method and path | Success | Other status codes and error codes |
| --- | --- | --- |
| `GET /markets` | 200 | None. |
| `GET /prices/current` | 200; a valid crop with no observations returns an empty `items` list. | 422 `validation_error` for an invalid crop. |
| `GET /prices/trends` | 200 | 404 `market_not_available`; 422 `validation_error` for invalid query parameters. |
| `GET /forecasts` | 200 | 404 `market_not_available`; 404 `forecast_unavailable` with reason `no_forecast` or `stale`; 422 `validation_error`. |
| `POST /listings` | 201 | 404 `market_not_available`; 409 `listing_limit_reached`; 413 `image_too_large`; 422 `invalid_image` or `validation_error`. |
| `GET /listings/mine` | 200 | 422 `validation_error` for invalid filters or pagination. |
| `PATCH /listings/{id}` | 200 | 404 `listing_not_found`; 409 `listing_removed`; 413 `image_too_large`; 422 `no_changes`, `invalid_image`, or `validation_error`. |
| `DELETE /listings/{id}` | 204 | 404 `listing_not_found`; 409 `listing_has_pending_orders`. Deleting an already removed listing also returns 204. |
| `GET /listings/mine/orders` | 200 | 422 `validation_error` for invalid filters or pagination. |
| `GET /listings/mine/{id}` | 200 (any status, including `sold_out` and `removed`) | 404 `listing_not_found` (another farmer's listing and an unknown id give identical bodies); 422 `validation_error` for a non-UUID id. Buyers and admins get 403. |

Errors use `{"error":{"code":"...","message":"...","request_id":"..."}}`; forecast errors may also include `reason`.

`POST /listings` enforces the active-listing limit and returns `listing_limit_reached` (409), `invalid_image` (422), or `image_too_large` (413) when applicable. Listing edits/removals enforce ownership; both a missing listing and another farmer's listing return the same `listing_not_found` (404). Other listing errors are `listing_removed` (409), `listing_has_pending_orders` (409), and `no_changes` (422).

## Farmer API details

`GET /markets` returns only markets with at least one `market_prices` row:

```json
[{"name":"Kisumu","latest_month":"2026-09-01"},{"name":"Nairobi","latest_month":"2026-09-01"}]
```

`GET /prices/current?crop=maize` returns `{items, as_of_note}`. Each item contains `market`, `crop`, `month`, `avg_price_per_kg`, `source`, `fetched_at`, `previous_month`, `previous_price`, and `change_pct`. Previous values are null if the immediately preceding calendar month has no row; no crop data produces an empty `items` array.

```json
{
	"items": [
		{
			"market": "Nairobi",
			"crop": "maize",
			"month": "2026-09-01",
			"avg_price_per_kg": 45.25,
			"source": "fews",
			"fetched_at": "2026-10-01T08:00:00Z",
			"previous_month": "2026-08-01",
			"previous_price": 44.0,
			"change_pct": 2.84
		}
	],
	"as_of_note": "Prices are monthly figures from FEWS NET; the newest published month is shown."
}
```

`GET /prices/trends?market=Nairobi&crop=maize&months=24` returns `{market, crop, points, first_month, last_month, count}`. Each point contains `month`, `avg_price_per_kg`, and `source`; absent months remain absent.

```json
{
	"market": "Nairobi",
	"crop": "maize",
	"points": [
		{"month": "2026-08-01", "avg_price_per_kg": 44.0, "source": "fews"},
		{"month": "2026-09-01", "avg_price_per_kg": 45.25, "source": "fews"}
	],
	"first_month": "2026-08-01",
	"last_month": "2026-09-01",
	"count": 2
}
```

`GET /forecasts?market=Nairobi&crop=maize&horizon=one_month` passes through stored forecast values and SHAP JSON and adds `expected_change_pct`, `based_on`, and `for_month`. A forecast is available only when its `origin_month` matches the latest observed price month for the series and that price month is no more than two calendar months old in `Africa/Nairobi`. Missing data returns `404 forecast_unavailable` with reason `no_forecast`; a mismatched or stale origin returns that code with reason `stale`. Forecasts are never generated or modified by the API.

```json
{
	"market": "Nairobi",
	"crop": "maize",
	"horizon": "one_month",
	"origin_month": "2026-09-01",
	"target_month": "2026-10-01",
	"origin_price": 45.25,
	"predicted_price": 46.38,
	"predicted_log_change": 0.0247,
	"expected_change_pct": 2.5,
	"shap_values": {"rainfall": 0.17},
	"generated_at": "2026-09-01T08:00:00Z",
	"based_on": "2026-09-01",
	"for_month": "2026-10-01"
}
```

Listing responses use `ListingOut`: `id`, `farmer_id`, `crop`, `market`, `quantity_available`, `unit`, `price_per_unit`, `image_url`, `status`, `created_at`, and `updated_at`. They do not expose storage paths. `GET /listings/mine` includes removed listings unless filtered by status. On edits, `quantity_available` means currently available stock after reservations have already been deducted; price changes apply only to future orders.

```json
{
	"id": "123e4567-e89b-12d3-a456-426614174000",
	"farmer_id": "123e4567-e89b-12d3-a456-426614174001",
	"crop": "maize",
	"market": "Nairobi",
	"quantity_available": 25.0,
	"unit": "kg",
	"price_per_unit": 45.5,
	"image_url": null,
	"status": "active",
	"created_at": "2026-10-01T08:00:00Z",
	"updated_at": "2026-10-01T08:00:00Z"
}
```

#### Details: `GET /listings/mine/{id}`

Path parameter: `listing_id` (UUID). Returns the farmer's own listing as `ListingOut`, exactly as it appears in `GET /listings/mine` (same fields, and `image_url` is built the same way). Every status is returned, so the edit screen can load a `sold_out` listing or show a `removed` one read-only. It is a plain read: it takes no lock and never contains buyer, order or payment data.

| Status | Body |
| --- | --- |
| 200 | `ListingOut`, for example the object above with `"status": "removed"`. |
| 401 | `invalid_credentials` without a valid token. |
| 403 | `forbidden` for buyers and admins. |
| 404 | `{"error": {"code": "listing_not_found", "message": "Listing not found.", "request_id": "..."}}`, identical for another farmer's listing and an unknown id. |
| 422 | `validation_error` when `listing_id` is not a UUID. |

`GET /listings/mine/orders` returns paginated `items` and a `note`. Items include the order snapshot, buyer name, and latest payment status/environment/receipt/completion time. Buyer phone, email, and ID, and payment idempotency/request IDs and result descriptions, are never returned.

```json
{
	"items": [
		{
			"order_id": "123e4567-e89b-12d3-a456-426614174002",
			"reference": "AB12CD34",
			"listing_id": "123e4567-e89b-12d3-a456-426614174000",
			"crop": "maize",
			"market": "Nairobi",
			"quantity": 2.0,
			"unit_price": 45.5,
			"total_amount": 91.0,
			"status": "paid",
			"created_at": "2026-10-01T08:00:00Z",
			"buyer_name": "Amina",
			"payment_status": "completed",
			"payment_environment": "sandbox",
			"mpesa_receipt_number": "QWE12345",
			"payment_completed_at": "2026-10-01T08:01:00Z"
		}
	],
	"total": 1,
	"limit": 20,
	"offset": 0,
	"note": "A paid status is a simulated sandbox payment confirmation; delivery is arranged outside the platform."
}
```

## Buyer marketplace API

Only buyers use these routes, except that an admin may read a single order. Buyers cannot see prices or forecasts. The client never sends a role, buyer id, price, total, or status; all are derived on the server.

Stock is reserved at checkout: the ordered quantity is removed from the listing immediately and the order stays `pending_payment` for `ORDER_EXPIRY_MINUTES` (default 5), measured by the database clock. A scheduled database job expires unpaid orders and returns their stock. A buyer can hold at most `MAX_PENDING_ORDERS_PER_BUYER` (default 3) unexpired pending orders, and only one per listing. Payment uses `POST /orders/{id}/pay` (below).

| Method and path | Roles | Success | Other status codes and error codes |
| --- | --- | --- | --- |
| `GET /listings` | Buyer | 200 | 422 `validation_error` (bad crop, unknown market, negative or reversed price range, bad sort or pagination). |
| `GET /listings/markets` | Buyer | 200 (`[]` when nothing is listed) | None besides 401 and 403. |
| `GET /listings/{id}` | Buyer | 200 | 404 `listing_not_found` (removed or unknown, identical bodies); 422 for a non-UUID id. |
| `POST /orders` | Buyer | 201 | 404 `listing_not_found`; 409 `listing_unavailable`, `insufficient_stock`, `pending_order_exists` (with `existing_order_id`), `too_many_pending_orders`; 422 `order_too_small` or `validation_error`; 429 `rate_limited` (30 per hour per IP). |
| `GET /orders/mine` | Buyer | 200 | 422 `validation_error` for an invalid status or pagination. |
| `GET /orders/{id}` | Owning buyer, admin | 200 | 404 `order_not_found` (another buyer's order and an unknown id give identical bodies). |
| `POST /orders/{id}/cancel` | Owning buyer | 200 (also for an already cancelled order) | 404 `order_not_found`; 409 `order_not_cancellable` (paid, expired, or any other non-pending status), `payment_in_progress`; 429 `rate_limited` (30 per hour per IP). |
| `POST /orders/{id}/pay` | Owning buyer | 202 (also for a replay of the same key) | 404 `order_not_found`; 409 `order_not_payable`, `order_expiring` (`reason` `expired` or `too_close`), `payment_in_progress`, `max_attempts_reached`, `idempotency_key_reused`; 422 `idempotency_key_required`, `invalid_idempotency_key`, `invalid_phone`, `phone_required`, `validation_error`; 429 `too_many_payment_attempts`, `too_many_buyer_payment_attempts`, `too_many_phone_numbers` or `rate_limited` (10 per minute per IP), all with `Retry-After`; 502 `payment_request_failed`; 503 `payments_unavailable`. |

All routes return 401 without a valid token and 403 `forbidden` for other roles. Farmers and admins get 403 on `GET /listings`, `GET /listings/markets` and `GET /listings/{id}`; buyers get 403 on every farmer listing route.

A listing is visible to buyers when it is `active`, has `quantity_available > 0` and its farmer is not disabled. This one rule (`buyer_visible_listings_clause()` in `app/services/listing_service.py`) decides what `GET /listings` returns, what `GET /listings/markets` counts, the `available` flag of `GET /listings/{id}` and whether `POST /orders` accepts the listing.

#### Details: `GET /listings`

Query parameters: `crop` (`maize` or `beans`), `market` (must exist in `market_locations`, case-insensitive), `min_price` and `max_price` (non-negative decimals, `min_price <= max_price`), `sort` (`newest` default, `price_asc`, `price_desc`; ties are broken by id), `limit` (1-100, default 20), `offset`. Only `active` listings with `quantity_available > 0` are returned.

```json
{
	"items": [
		{
			"id": "123e4567-e89b-12d3-a456-426614174000",
			"crop": "maize",
			"market": "Nairobi",
			"quantity_available": 10.0,
			"unit": "kg",
			"price_per_unit": 40.0,
			"image_url": null,
			"seller_name": "Jane Wanjiku",
			"status": "active",
			"created_at": "2026-10-13T08:00:00Z"
		}
	],
	"total": 1,
	"limit": 20,
	"offset": 0
}
```

Listings show the seller's name only, never the farmer's id, phone number, or email.

#### Details: `GET /listings/markets`

No parameters. Returns the markets that currently have at least one buyer-visible listing, ordered by market name, so the browse market filter offers only choices that return results. It is one grouped query over `produce_listings` (joined to `users` for the disabled-farmer rule). Nothing comes from `market_prices`: no prices, no `latest_month`, and a market with price data but no listings is not returned.

```json
[
	{"name": "Kisumu", "listing_count": 2},
	{"name": "Nairobi", "listing_count": 7}
]
```

With nothing listed the response is `[]`. The sum of `listing_count` equals the `total` of an unfiltered `GET /listings`, and each count equals the `total` of `GET /listings?market=<name>`. Status codes: 200; 401 `invalid_credentials` without a valid token; 403 `forbidden` for farmers and admins.

#### Details: `GET /listings/{id}`

Returns the same fields plus `available` (true when the listing is active and has stock). A `sold_out` listing is returned with `available: false`.

#### Details: `POST /orders`

Body (unknown fields are rejected with 422):

```json
{"listing_id": "123e4567-e89b-12d3-a456-426614174000", "quantity": "2.5"}
```

`quantity` is a string or number: finite, greater than 0, at most two decimal places, at least `MIN_ORDER_QUANTITY` (default 1), at most 100000. `unit_price` is the listing's current price, and `total_amount = ceil(quantity × unit_price)` in whole shillings; a total below `MIN_ORDER_TOTAL_KES` (default 10) returns 422 `order_too_small`. Ordering the remaining stock marks the listing `sold_out`. If the buyer already has a pending order for the listing, the 409 body contains its id so the client can continue with it:

```json
{"error": {"code": "pending_order_exists", "message": "You already have an order awaiting payment for this listing.", "existing_order_id": "123e4567-e89b-12d3-a456-426614174009"}}
```

### Order response (`OrderOut`)

```json
{
	"id": "123e4567-e89b-12d3-a456-426614174009",
	"reference": "K7QX2MPA",
	"status": "pending_payment",
	"is_expired": false,
	"quantity": 2.5,
	"unit_price": 112.56,
	"total_amount": 282,
	"expires_at": "2026-10-13T09:05:00Z",
	"server_time": "2026-10-13T09:00:00Z",
	"created_at": "2026-10-13T09:00:00Z",
	"can_pay": true,
	"can_cancel": true,
	"listing": {
		"id": "123e4567-e89b-12d3-a456-426614174000",
		"crop": "maize",
		"market": "Nairobi",
		"unit": "kg",
		"image_url": null,
		"seller_name": "Jane Wanjiku",
		"status": "active"
	},
	"payment": null,
	"note": "Payments run in the Safaricom sandbox. A paid order is a simulated confirmation, no real money moves, and delivery is arranged outside the platform."
}
```

- `server_time` is the database time; use it with `expires_at` for a countdown instead of the device clock.
- `is_expired` is true for a `pending_payment` order whose `expires_at` has passed, even before the expiry job has run.
- `payment` is null, or the latest payment: `status`, `status_message`, `phone_masked` (the M-Pesa number the buyer entered for that payment, for example `254*******678`), `stk_target_masked` (the number the prompt was sent to, for example `254*******149`; the same as `phone_masked` unless the server runs with `MPESA_STK_TARGET=test_msisdn`), `mpesa_receipt_number`, `result_desc`, `completed_at`, and `attempts` (number of payment rows). `status_message` is a fixed English text per status: `initiating` "Sending the payment request", `initiated` "Waiting for you to confirm on your phone", `completed` "Payment received", `cancelled` "You cancelled the payment request", `timeout` "The request timed out", `failed` "The payment failed". Payment identifiers, idempotency keys, checkout and merchant request ids, full phone numbers, and callback data are never returned.
- `can_pay` is true only for the owning buyer, when the order is `pending_payment`, not expired, has at least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` (default 90) seconds left on the database clock, has no open (`initiating` or `initiated`) payment, and has fewer than `MPESA_MAX_ATTEMPTS_PER_ORDER` (default 3) payment rows. `can_cancel` is true for the owning buyer when the order is `pending_payment` with no open payment. Both are false for an admin.
- When the owning buyer reads `GET /orders/{id}`, the latest payment is reconciled first: a stale `initiating` payment is failed, and an `initiated` payment older than `RECONCILE_AFTER_SECONDS` (or a `timeout` payment younger than `MPESA_TIMEOUT_RECONCILE_WINDOW_MINUTES`) is checked with an STK Push Query, at most once every `MPESA_QUERY_MIN_INTERVAL_SECONDS`. A reconciliation failure never fails the request. Admin reads never reconcile.

#### Details: `GET /orders/mine`

Query parameters: optional `status` (an order status), `limit` (1-100, default 20), `offset`. Returns `{items, total, limit, offset}` of `OrderOut`, newest first.

#### Details: `POST /orders/{id}/cancel`

Cancels the buyer's own `pending_payment` order and returns its quantity to the listing; a `sold_out` listing becomes `active` again (a removed listing gets its quantity back but stays removed). Cancelling an already cancelled order returns 200 with the current order.

#### Details: `POST /orders/{id}/pay`

Sends an M-Pesa STK Push prompt for the order through the Safaricom Daraja **sandbox** (shortcode 174379). No real money moves; a `paid` order is a simulated confirmation.

Headers: `Authorization: Bearer <token>` and `Idempotency-Key` (required, 8 to 64 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`). Rate limit: 10 per minute per IP.

Body (required): the M-Pesa number to send the prompt to. Accounts have no phone number, so this is the only source of the number:

```json
{ "phone_number": "0712 345 678" }
```

`phone_number` is a string of at most 20 characters (after trimming); `07...`, `01...`, `+2547...`, `2547...` and `2541...` are accepted, with spaces or dashes, and stored as `254XXXXXXXXX` on the payment (`payments.phone_number`). A missing body, a missing field, `null` or an empty value gives 422 `phone_required` before any database work. Any other field, a body that is not a JSON object, or a value longer than 20 characters gives 422 `validation_error`. A number that is not a valid Safaricom number gives 422 `invalid_phone`. The number is used for this payment only and is not verified (anyone can enter any number). The client never sends an amount or order data: the amount is the order total. The prompt goes to the entered number, or to the sandbox test number when `MPESA_STK_TARGET=test_msisdn` (`registered` is an alias of `entered`).

The key is stored scoped to the buyer and order, so it cannot collide with or probe other buyers' keys. Repeating a request with the same key and the same number (any accepted format) returns 202 with the current order and never sends a second prompt, even after the order has moved on. A replay with a different number gives 409 `idempotency_key_reused`; a replay without a number gives 422 `phone_required`. Use a new key for a new attempt.

The payment can start only when the order is `pending_payment`, at least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` (default 90) seconds remain before `expires_at`, no other payment is open, and the order has fewer than `MPESA_MAX_ATTEMPTS_PER_ORDER` (default 3) attempts. An `initiating` payment whose request was never sent (older than `MPESA_STALE_INITIATING_SECONDS`, default 60) is failed and does not block a new attempt. After those checks three limits apply, measured on the database clock:

- the entered number has fewer than `MPESA_PHONE_ATTEMPTS_PER_HOUR` (default 5) payments in the last hour;
- the buyer has fewer than `MPESA_BUYER_ATTEMPTS_PER_HOUR` (default 10) payments, across all orders, in the last hour;
- the entered number was already used by the buyer in the last 24 hours, or the buyer has used fewer than `MPESA_BUYER_DISTINCT_PHONES_PER_DAY` (default 3) different numbers in that time.

Each limit answers 429 with `Retry-After` in whole seconds: for an hourly limit, the time until the oldest counted payment leaves the hour; for the distinct numbers limit, the time until the first of the buyer's numbers leaves the 24 hours.

Response 202 (`OrderOut` plus two fields):

```json
{
	"id": "123e4567-e89b-12d3-a456-426614174009",
	"reference": "K7QX2MPA",
	"status": "pending_payment",
	"can_pay": false,
	"can_cancel": false,
	"payment": {
		"status": "initiated",
		"status_message": "Waiting for you to confirm on your phone",
		"phone_masked": "254*******678",
		"stk_target_masked": "254*******149",
		"mpesa_receipt_number": null,
		"result_desc": null,
		"completed_at": null,
		"attempts": 1
	},
	"message": "Check your phone and enter your M-Pesa PIN.",
	"prompt": "Prompt sent to 254*******149"
}
```

(Other `OrderOut` fields are omitted above. The example ran with `MPESA_STK_TARGET=test_msisdn`, so the prompt went to the sandbox test number.) Then poll `GET /orders/{id}` until the payment is final.

Errors:

| Status | Code | When |
| --- | --- | --- |
| 404 | `order_not_found` | Unknown order or another buyer's order (identical bodies). |
| 409 | `order_not_payable` | The order is not `pending_payment`. |
| 409 | `order_expiring` | Fewer than 90 seconds remain; `reason` is `expired` or `too_close`. |
| 409 | `payment_in_progress` | Another payment for the order is `initiating` or `initiated`. |
| 409 | `max_attempts_reached` | The order already has the maximum number of payment attempts. |
| 409 | `idempotency_key_reused` | The key was already used with a different number ("This request was already used with a different number. Start a new payment attempt."). |
| 422 | `idempotency_key_required` / `invalid_idempotency_key` | Missing or malformed `Idempotency-Key`. |
| 422 | `invalid_phone` | The number is not a valid Safaricom number ("Enter a valid Safaricom number, for example 0712 345 678."). |
| 422 | `phone_required` | A missing body, a missing `phone_number`, `null` or an empty value ("Enter the M-Pesa number to send the payment prompt to."). |
| 422 | `validation_error` | Unknown body field, a body that is not a JSON object, or a `phone_number` longer than 20 characters. |
| 429 | `too_many_payment_attempts` | The entered number has `MPESA_PHONE_ATTEMPTS_PER_HOUR` payments in the last hour ("Too many payment attempts for this phone number. Please try again later."). |
| 429 | `too_many_buyer_payment_attempts` | The buyer has `MPESA_BUYER_ATTEMPTS_PER_HOUR` payments in the last hour ("You have made too many payment attempts. Please try again later."). |
| 429 | `too_many_phone_numbers` | A new number after `MPESA_BUYER_DISTINCT_PHONES_PER_DAY` different numbers in 24 hours ("You have used too many different phone numbers today. Please try again tomorrow or use one you already used."). |
| 429 | `rate_limited` | More than 10 requests per minute from one IP. |
| 502 | `payment_request_failed` | Daraja rejected the request or did not answer in time ("Could not start the M-Pesa prompt. Please try again."). After a timeout the prompt may still have arrived, so a duplicate prompt is possible in the sandbox. |
| 503 | `payments_unavailable` | Payment settings are missing or `MPESA_CALLBACK_BASE_URL` is not HTTPS. |

#### Details: `POST /payments/callback/{token}`

Called by Daraja only, never by the PWA. There is no bearer authentication: the secret `MPESA_CALLBACK_TOKEN` in the path is the check, and a wrong token returns 404 with an empty body. Bodies over `CALLBACK_MAX_BYTES` (default 16384) return 413. After a valid token the endpoint always returns 200 with `{"ResultCode": 0, "ResultDesc": "Accepted"}`, also for malformed, unknown, duplicate or mismatched callbacks, so Daraja never retries. Every callback is stored as a payment event first. The phone number in the callback is ignored. There is no rate limit on this route. The server access log shows the path as `/api/v1/payments/callback/***`.

## Admin API

All routes are under `/api/v1/admin` and require an admin account; the role is read from the database on every request. Every route returns 401 without a valid token, 403 `forbidden` for farmers and buyers, and 403 `password_change_required` until the account's password has been changed (the seeded super admin's first login). Pagination uses `limit` (1-100, default 20) and `offset`, and returns `{items, total, limit, offset}`. Search `q` is 2 to 50 characters; `%` and `_` match literally.

Rate limits per IP: the four invite routes 10 per hour each; `POST /admin/email/test` 5 per hour; disable, enable, listing removal and order resolution 30 per hour (429 `rate_limited`).

No route ever returns a credential. New admins are invited by an emailed code and choose their own password; anyone who forgets a password resets it by email (see [Invites and password reset](#invites-and-password-reset)). Admin responses never contain password hashes, codes or code hashes, token hashes, idempotency keys, merchant request ids, Daraja credentials or full payment phone numbers (entered numbers and payment targets are masked, for example `254*******149`). Accounts have no phone number. Users' emails appear only in the user routes; invite routes show masked emails (`g***@e***.com`).

### Status and error codes

| Route | Success | Errors |
| --- | --- | --- |
| `POST /admin/invites` | 201 | 403 `forbidden` (not the super admin); 409 `user_exists`, `too_many_pending_invites`, `invite_conflict`; 422 `validation_error`; 503 `codes_unavailable` |
| `POST /admin/invites/{id}/resend` | 200 | 403 `forbidden`; 404 `invite_not_found`; 409 `invite_not_pending`, `send_limit_reached`; 429 `resend_cooldown`; 422 `body_not_allowed`; 503 `codes_unavailable` |
| `GET /admin/invites` | 200 | 403 `forbidden`; 422 `validation_error` |
| `DELETE /admin/invites/{id}` | 204 (also when already revoked) | 403 `forbidden`; 404 `invite_not_found`; 409 `invite_not_pending` (already used) |
| `POST /admin/email/test` | 200 | 403 `forbidden`; 409 `no_email`; 422 `body_not_allowed`; 502 `email_failed` with `category` |
| `GET /admin/users` | 200 | 422 `validation_error` |
| `PATCH /admin/users/{id}/disable`, `/enable` | 200 | 403 `cannot_modify_super_admin`, `cannot_disable_self`, `forbidden` (ordinary admin on an admin); 404 `user_not_found`; 422 `body_not_allowed` |
| `GET /admin/listings` | 200 | 422 `validation_error` |
| `DELETE /admin/listings/{id}` | 204 (also when already removed) | 404 `listing_not_found`; 409 `listing_has_open_payments` |
| `GET /admin/orders`, `GET /admin/orders/{id}` | 200 | 404 `order_not_found` |
| `POST /admin/orders/{id}/resolve` | 200 | 404 `order_not_found`; 409 `order_not_in_reconciliation`, `reconciliation_context_missing`, `insufficient_stock_to_confirm`; 422 `validation_error` |
| `GET /admin/payments` | 200 | 422 `validation_error` |
| `GET /admin/payments/{id}/events` | 200 | 404 `payment_not_found` |
| `GET /admin/stats`, `GET /admin/forecast-accuracy` | 200 | 422 `validation_error` |

### Invites (super admin only)

`POST /admin/invites`. Body, unknown fields rejected (so no `role`, `is_super_admin` or other flag can be sent):

```json
{"name": "Grace Admin", "email": "grace@example.com"}
```

The email is lowercased; a `phone_number` field is rejected with 422 like any unknown field. In one transaction the server:

1. Refuses with 409 `user_exists` when an account already has this email.
2. Revokes any earlier pending invite for the same email, so the newest invite replaces it.
3. Refuses with 409 `too_many_pending_invites` when `INVITE_MAX_PENDING` (10) invites are pending.
4. Stores the invite with an HMAC of a new 8-character code, `expires_at = now() + INVITE_TTL_DAYS` (7 days) and `code_expires_at = now() + INVITE_CODE_TTL_MINUTES` (15 minutes), using the database clock.

After the commit the code is emailed to the invited address. The response (`Cache-Control: no-store`) never contains the code:

```json
{
	"id": "6f1c0e8a-2b44-4c55-9a1e-6c2f8d0b7e11",
	"name": "Grace Admin",
	"email_masked": "g***@e***.com",
	"expires_at": "2026-10-22T09:00:00Z",
	"code_expires_at": "2026-10-15T09:15:00Z",
	"email_sent": true,
	"message": "Invitation sent. The code in the email is valid for 15 minutes."
}
```

If the email cannot be sent, the invite is kept, `email_sent` is `false`, and the message says to use the resend route.

`POST /admin/invites/{id}/resend` (empty body) emails a new code for a pending invite; the previous code stops working. It returns 409 `invite_not_pending` when the invite is used, revoked, locked or expired, 429 `resend_cooldown` within `INVITE_RESEND_COOLDOWN_SECONDS` (60) of the last send, and 409 `send_limit_reached` after `INVITE_MAX_SENDS` (5) sends. Resending never resets the attempt counter.

`GET /admin/invites` filters by `status` (`pending`, `used`, `expired`, `revoked`, `locked`). Items: `id, name, email_masked, status, created_by_name, created_at, expires_at, send_count, attempts, used_at, revoked_at`.

`DELETE /admin/invites/{id}` revokes a pending (also an expired or locked) invite and returns 204; repeating it returns 204; a used invite gives 409 `invite_not_pending`.

`POST /admin/email/test` (empty body) sends a short message without any code or secret to the acting super admin's own email address and returns `{"email_sent": true}`. On failure it returns 502 with only a category, never the provider's text:

```json
{"error": {"code": "email_failed", "message": "The email could not be sent.", "category": "auth_failed"}}
```

Categories: `connection_timeout`, `auth_failed`, `tls_failed`, `recipient_refused`, `invalid_header`, `not_configured`, `other`. When `INVITE_CODE_PEPPER` is not set, the invite create and resend routes and every public code route (registration, sign in codes, invite acceptance, password reset) return 503 `codes_unavailable`; the rest of the API is unaffected. Because sign in needs an emailed code for the roles in `LOGIN_OTP_ROLES`, use the console provider (or `scripts/admin_recovery.py issue-login-code`) for the very first super admin sign in before this check can be run.

### Users

`GET /admin/users` filters by `role`, `is_disabled` and `q` (case-insensitive name or email; 2 to 50 characters, `%` and `_` match literally). Items are `UserAdminOut`:

```json
{
	"id": "6f1c0e8a-2b44-4c55-9a1e-6c2f8d0b7e11",
	"name": "Grace Admin",
	"email": "grace@example.com",
	"email_verified_at": "2026-10-15T09:00:00Z",
	"role": "admin",
	"is_super_admin": false,
	"is_disabled": false,
	"disabled_at": null,
	"disabled_by_name": null,
	"must_change_password": false,
	"created_at": "2026-10-15T09:00:00Z",
	"created_by_name": "Super Admin",
	"listing_count": null,
	"order_count": null
}
```

`listing_count` is set for farmers and `order_count` for buyers, otherwise null.

`PATCH /admin/users/{id}/disable` sets `is_disabled`, `disabled_at` and `disabled_by`; `enable` clears all three. Neither applies to the super admin or to yourself, and only the super admin can disable or enable another admin. Repeating a call returns 200 unchanged. A disabled user's next request returns 401.

There is no admin password reset route. A user who forgets a password uses the email reset below; to lock someone out immediately, disable the account.

### Listings

`GET /admin/listings` returns `AdminListingOut`: `id, farmer_id, farmer_name, farmer_disabled, crop, market, quantity_available, unit, price_per_unit, image_url, status, removed_at, removed_by_name, pending_orders, paid_orders, created_at`. `q` matches the farmer's name.

`DELETE /admin/listings/{id}` soft-removes the listing: its `pending_payment` orders are cancelled and their quantities returned, then the listing becomes `removed` with `removed_at` and `removed_by`. No row or image is deleted. While a buyer is paying for one of those orders (an `initiating` or `initiated` payment) it returns 409 `listing_has_open_payments`; try again after `ORDER_EXPIRY_MINUTES`.

Listings of a disabled farmer are hidden from buyers (`GET /listings` excludes them; `GET /listings/{id}` and `POST /orders` return 404 `listing_not_found`) and reappear when the farmer is enabled.

### Orders and payments

`GET /admin/orders` filters by `status` and `q` (order reference prefix or buyer name). `AdminOrderOut`:

```json
{
	"id": "123e4567-e89b-12d3-a456-426614174009",
	"reference": "K7QX2MPA",
	"status": "needs_reconciliation",
	"buyer_name": "Buyer Ann",
	"seller_name": "Jane Wanjiku",
	"listing_id": "123e4567-e89b-12d3-a456-426614174000",
	"crop": "maize",
	"market": "Nairobi",
	"quantity": 2.5,
	"unit_price": 45.5,
	"total_amount": 114,
	"created_at": "2026-10-15T09:00:00Z",
	"expires_at": "2026-10-15T09:05:00Z",
	"latest_payment_status": "completed",
	"latest_mpesa_receipt_number": "SBX1A2B3C4",
	"reconciliation": {
		"reason": "late_success_after_expiry",
		"order_status_before": "expired",
		"flagged_at": "2026-10-15T09:06:10Z"
	},
	"resolution": null
}
```

`reconciliation` is set only for `needs_reconciliation` orders, from the latest `error` payment event that records `order_status_before`. Reasons: `late_success_after_expiry`, `success_on_closed_order`, `success_on_closed_payment`, `amount_mismatch`.

`GET /admin/orders/{id}` adds `payments` (each `id, status, amount, phone_masked, stk_target_masked, mpesa_receipt_number, result_code, result_desc, environment, created_at, completed_at, callback_received_at`) and `resolution` (`{action, note, admin_name, created_at}` from the latest `admin_action` event) when one exists.

`POST /admin/orders/{id}/resolve` body, unknown fields rejected:

```json
{"action": "confirm_sale", "note": "Buyer showed the M-Pesa confirmation message."}
```

`action` is `confirm_sale` or `void_order`; `note` is `RESOLVE_NOTE_MIN_LENGTH` (default 10) to 500 characters. Only `needs_reconciliation` orders can be resolved, so a repeated request returns 409 `order_not_in_reconciliation`. Stock depends on the order status before it was flagged:

| Action | Flagged while `pending_payment` (stock still reserved) | Flagged after `expired` or `cancelled` (stock returned) | Order becomes |
| --- | --- | --- | --- |
| `confirm_sale` | no stock change | quantity re-reserved; 409 `insufficient_stock_to_confirm` if not enough is left | `paid` |
| `void_order` | quantity returned to the listing | no stock change | `cancelled` |

A removed listing is never changed. Payment rows are left as they are; a voided sale's refund is arranged outside the platform. The resolution is recorded as an `admin_action` payment event in the same transaction, and the response is the order detail with `resolution`.

`GET /admin/payments` filters by `status`, `order_reference` (exact), `environment` (`sandbox` or `production`), and `created_from` / `created_to` (dates in Africa/Nairobi). Items: `id, order_id, order_reference, order_status, buyer_name, amount, status, phone_masked, stk_target_masked, mpesa_receipt_number, result_code, result_desc, environment, created_at, completed_at, callback_received_at, attempt_number`.

`phone_masked` is the number the buyer entered for that payment and `stk_target_masked` the number the prompt was sent to (the sandbox test number with `MPESA_STK_TARGET=test_msisdn`); both keep only their last three digits. One item, from a server running with `test_msisdn`:

```json
{
	"id": "5b0e8f1c-2f7e-4a8e-9a43-0c2b1d7e9f10",
	"order_id": "123e4567-e89b-12d3-a456-426614174009",
	"order_reference": "K7QX2MPA",
	"order_status": "paid",
	"buyer_name": "Buyer Ann",
	"amount": 114,
	"status": "completed",
	"phone_masked": "254*******678",
	"stk_target_masked": "254*******149",
	"mpesa_receipt_number": "SBX1A2B3C4",
	"result_code": 0,
	"result_desc": "The service request is processed successfully.",
	"environment": "sandbox",
	"created_at": "2026-10-15T09:00:00Z",
	"completed_at": "2026-10-15T09:00:40Z",
	"callback_received_at": "2026-10-15T09:00:40Z",
	"attempt_number": 1
}
```

`GET /admin/payments/{id}/events` returns `{payment_id, items, limit}` where each item is `{id, event_type, checkout_request_id, payload, created_at}`, oldest first, at most 100. Payloads were redacted when stored and are returned unchanged.

### Statistics

`GET /admin/stats`:

```json
{
	"users": {"farmers": 12, "buyers": 30, "admins": 2, "disabled": 1},
	"listings": {"active": 9, "sold_out": 3, "removed": 1},
	"orders": {"pending_payment": 1, "paid": 20, "payment_failed": 0, "expired": 6, "cancelled": 2, "needs_reconciliation": 1},
	"payments": {"initiating": 0, "initiated": 1, "completed": 21, "failed": 3, "cancelled": 2, "timeout": 4},
	"completed_sandbox_amount_kes": 2394,
	"attention": {"orders_needing_reconciliation": 1, "payments_initiated_over_10_minutes": 0},
	"pipeline": {
		"series": [
			{"market": "Nairobi", "crop": "maize", "latest_price_month": "2026-09-01", "forecast_origin_month": "2026-09-01", "forecast_current": true}
		],
		"last_prices_fetch": "2026-10-02T06:00:00Z",
		"last_forecast_generated": "2026-10-02T06:10:00Z"
	}
}
```

Every status appears, with 0 when there are none. `completed_sandbox_amount_kes` sums completed sandbox payments only. `pipeline.series` has one row per market with price data and crop; `forecast_current` is true when the latest one-month forecast's origin equals the latest price month.

### Forecast accuracy

`GET /admin/forecast-accuracy` filters by `market`, `crop`, `horizon`, `is_backfill` and `include_pending` (default true), ordered by `origin_month` then `target_month`, newest first. `items` are rows of the `forecast_accuracy` view (`market, crop, horizon, origin_month, target_month, origin_price, predicted_price, actual_price, abs_error, pct_error, actual_log_change, direction_correct, persistence_abs_error, beat_persistence, is_backfill, is_pending`); error columns are null while a row is pending. `summary` has one entry per `(horizon, is_backfill)` with `n_realised`, `n_pending`, `mape` (mean absolute percentage error), `mean_abs_error`, `directional_accuracy` and `beat_persistence_rate` (shares from 0 to 1), computed over realised rows and rounded to 2 decimals. `note` explains that backfilled forecasts are reported separately and that sample sizes are small.

## Authentication details

Accounts are identified only by a verified email address; there is no phone number on any account or invitation (a phone number exists only on a payment). Farmers and buyers confirm their email with a code before the account exists, and every password sign in for the roles in `LOGIN_OTP_ROLES` (default `farmer,buyer,admin`; production must include `admin`) needs a second step: a code emailed after the password is verified. Access tokens last `ACCESS_TOKEN_EXPIRE_MINUTES` (default 480, 8 hours).

Codes are 8 characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789`, shown in emails as `XXXX-XXXX`; spaces, hyphens and lower case are accepted. A code is never put in a URL, a subject line, a response or a log, and is stored only as an HMAC keyed with `INVITE_CODE_PEPPER` (the setting keeps its historical name but protects every email code). The registration token and the login challenge token are 256 random bits, stored only as a SHA-256 digest. When `INVITE_CODE_PEPPER` is not set, every route that issues or checks a code returns 503 `codes_unavailable`.

### AuthResult

Every sign in endpoint (`POST /auth/register/verify`, `POST /auth/login`, `POST /auth/login/verify`, `POST /auth/accept-invite`) answers with `AuthResult`. Fields that do not apply to the `status` are omitted from the JSON. Responses carry `Cache-Control: no-store`.

Signed in:

```json
{
	"status": "signed_in",
	"access_token": "<signed-token>",
	"token_type": "bearer",
	"expires_in": 28800,
	"role": "farmer",
	"must_change_password": false,
	"is_super_admin": false
}
```

Code required (only from `POST /auth/login`):

```json
{
	"status": "code_required",
	"challenge_token": "<opaque token, keep in memory only>",
	"email_masked": "a***@e***.com",
	"code_expires_at": "2026-10-26T09:10:00Z",
	"resend_available_in": 60,
	"code_sent": true
}
```

`is_super_admin` and `must_change_password` are read from the users row, for the client UI only; the token does not carry them, and every protected route loads the user and its role from the database again. `code_sent` is `false` when the email could not be sent; the challenge is kept, so the user can resend.

### Registration

`POST /auth/register/farmer` and `POST /auth/register/buyer` (10 per hour per IP). Body, unknown fields rejected (so `role`, `phone_number`, `is_super_admin` and every other flag give 422); the role comes only from the route:

```json
{"name": "Amina Farmer", "email": "amina@example.com", "password": "A-long-private-passw0rd"}
```

`name` is 2 to 100 characters after trimming, `email` is lowercased. The password must be 8 to 128 characters with an uppercase letter, a lowercase letter, a digit and a symbol (422 `validation_error` listing the missing rules), and must pass the password policy for the role (422 `password_too_weak` with a `reason`; neither error reveals anything about any account).

The answer is always 202 with the same shape, whether or not the address has an account:

```json
{
	"message": "If this address can be registered, we have sent a code to it.",
	"registration_token": "<opaque token, keep in memory only>",
	"email_masked": "a***@e***.com",
	"code_expires_at": "2026-10-26T09:15:00Z",
	"resend_available_in": 60
}
```

- New address: a pending registration is stored (hashes only) and a code valid for `REG_CODE_TTL_MINUTES` (15) is emailed. The registration itself lasts `REG_TTL_MINUTES` (60).
- Submitting again for the same address and role: the token is rotated (the previous token stops working) and the name and password are replaced. The code is kept, and no email is sent, while it is younger than `REG_RESEND_COOLDOWN_SECONDS` (60) and valid; otherwise a new code is sent, up to `REG_MAX_SENDS` (5) emails per registration. A registration for the other role, or one that is locked or expired, is replaced by a new one.
- Address that already has an account: nothing is stored. The account owner gets an "account exists" email (no code, no link with a secret) at most once per `REG_EXISTS_NOTICE_MINUTES` (10) per address; this throttle is kept in memory per process. The response carries a fresh token that is stored nowhere, so it can never verify.

`POST /auth/register/verify` (5 per minute per IP):

```json
{"registration_token": "<token from the 202>", "code": "K7QX-2MPA"}
```

The attempt is counted in its own committed transaction before the code is compared; after `REG_MAX_ATTEMPTS` (5) wrong codes in total, across resends, the registration is locked and even the correct code is refused. A correct code claims the registration and creates the user in one transaction (`email_verified_at = now()`, `must_change_password = false`, `password_changed_at = now()`, `is_super_admin = false`) and returns 201 `AuthResult` with `status: "signed_in"`: the registration code counts as the second step, so no sign in code follows. Every failure (a dummy or unknown token, a wrong or expired code, a used, locked or replaced registration, an address that got an account in the meantime) returns the same response:

```json
{"error": {"code": "invalid_or_expired_code", "message": "That code is incorrect or has expired. Request a new code or start again."}}
```

`POST /auth/register/resend` (5 per hour per IP) with `{"registration_token": "..."}` always returns 202 `{"message": "If this address can be registered, we have sent a code to it.", "resend_available_in": 60}`, for a real, dummy or unknown token alike. When the registration is open, unlocked and unexpired, the cooldown has passed and fewer than `REG_MAX_SENDS` codes were sent, a new code replaces the old one (the old code stops working; attempts are not reset) and is emailed in a background task.

### Sign in

`POST /auth/login` (5 per minute per IP):

```json
{"email": "amina@example.com", "password": "A-long-private-passw0rd"}
```

A wrong password, an unknown email and a disabled account return the same 401, and no email is sent:

```json
{"error": {"code": "invalid_credentials", "message": "Invalid email or password"}}
```

After `LOGIN_MAX_FAILURES` (5) failures for one email within `LOGIN_FAILURE_WINDOW_MINUTES` (15) the email is blocked with 429 `rate_limited` (per process). A role outside `LOGIN_OTP_ROLES` gets 200 `AuthResult` with `status: "signed_in"`. Otherwise, only after a correct password, the user's active challenge is superseded, a new challenge is created (`LOGIN_CHALLENGE_TTL_MINUTES`, 10; the code is valid for `LOGIN_CODE_TTL_MINUTES`, 10), the code is emailed synchronously (bounded by `SMTP_TIMEOUT_SECONDS`), and the answer is 200 `AuthResult` with `status: "code_required"`. More than `LOGIN_CHALLENGES_PER_HOUR` (10) challenges for one user in an hour gives 429 `too_many_login_codes` with `Retry-After` (the password was correct, so this reveals nothing).

`POST /auth/login/verify` (10 per minute per IP):

```json
{"challenge_token": "<token from the code_required result>", "code": "K7QX-2MPA"}
```

The attempt is counted (committed) before the comparison. Results:

| Status | Code | When |
| --- | --- | --- |
| 200 | | `AuthResult` with `status: "signed_in"`. |
| 400 | `invalid_or_expired_code` | Wrong code ("That code is incorrect or has expired."). The `LOGIN_MAX_ATTEMPTS` (5) wrong code locks the challenge. |
| 400 | `challenge_closed` | Unknown, used, superseded, locked or expired challenge, or a user disabled after the password step ("Start again from the sign in screen."). |

The challenge token alone is useless without the code and the code alone is useless without the token. A user with `must_change_password` still signs in; every route except `/auth/me` and `/auth/change-password` then answers 403 `password_change_required`.

`POST /auth/login/resend` (5 per hour per IP) with `{"challenge_token": "..."}`:

| Status | Code | When |
| --- | --- | --- |
| 200 | | `{"code_sent": true, "resend_available_in": 60, "code_expires_at": "..."}`; the old code stops working, attempts are not reset. |
| 429 | `resend_cooldown` | Within `LOGIN_RESEND_COOLDOWN_SECONDS` (60) of the last send; `Retry-After` gives the remaining seconds. |
| 409 | `send_limit_reached` | `LOGIN_MAX_SENDS` (3) codes were sent for this challenge. |
| 400 | `challenge_closed` | Unknown, used, locked or expired challenge. |

### Passwords and tokens

Registration and `new_password` on `/auth/change-password` must be 8-128 characters and include at least one uppercase letter, one lowercase letter, one digit and one non-whitespace symbol. Sign in accepts existing passwords without reapplying the creation policy.

New passwords chosen through registration, change-password, invite acceptance and password reset also pass the password policy (`app/core/password_policy.py`): admins need at least 12 characters, farmers and buyers at least 8, everyone at most 128; the password may not equal the account's email, the part of the email before `@` or the name (ignoring case), and may not be one of the common passwords in `app/core/common_passwords.txt`. A failure returns 422 with a short reason that never repeats the password:

```json
{"error": {"code": "password_too_weak", "message": "The password is too weak.", "reason": "Use at least 12 characters."}}
```

`POST /auth/change-password` accepts `current_password` and `new_password`, rejects a new password equal to the current password, and returns `200`:

```json
{"message": "Password changed. Log in again: your current access token is no longer accepted.", "login_required": true}
```

`GET /auth/me` returns `UserOut`: `id`, `name`, `email`, `role`, `is_super_admin`, `must_change_password`, `created_at` (no phone number, no password hash, no disabled-account details). Its `is_super_admin` comes from the row loaded for this request.

Access tokens carry their issue time (`iat`, whole seconds). Every password change (change-password, invite acceptance, password reset, registration, the seed and recovery scripts) sets `users.password_changed_at` from the database clock, and a token whose `iat` is earlier than the whole second of `password_changed_at` gets the same 401 as any invalid token.

Authentication error codes: `invalid_credentials` (401) is identical for unknown, incorrect-password and disabled accounts; `password_change_required` (403) blocks other authenticated routes until a forced password change is done; `forbidden` (403) indicates a role or super-admin failure; `rate_limited` (429) covers the IP and per email throttles; `too_many_login_codes` (429), `resend_cooldown` (429), `send_limit_reached` (409), `challenge_closed` (400) and `invalid_or_expired_code` (400) belong to the code steps; `codes_unavailable` (503) means `INVITE_CODE_PEPPER` is missing; `validation_error` (422) lists field names and safe details. On the payment route, `phone_required` (422) means the M-Pesa number is missing.

### Invites and password reset

These public routes need no token. When `INVITE_CODE_PEPPER` is not set, all four return 503 `codes_unavailable`.

`POST /auth/accept-invite` (5 per minute per IP). Body, unknown fields rejected:

```json
{"email": "grace@example.com", "code": "K7QX-2MPA", "password": "A-long-private-passw0rd"}
```

The password policy is checked first (admin rules) and a failure returns 422 `password_too_weak` that reveals nothing about any invite. The attempt is then counted in its own committed transaction, so a failed guess always counts; after `INVITE_MAX_ATTEMPTS` (10) wrong codes in total, across all resends, the invite is locked and even the correct code is refused. A correct code claims the invite and creates the admin in one transaction: `role=admin`, `is_super_admin=false`, `must_change_password=false`, `email_verified_at=now()`, `password_changed_at=now()`, `created_by` = the inviting super admin. The response is 201 `AuthResult` with `status: "signed_in"`: the invite code proves control of the email, so no sign in code follows. An unknown email, a wrong code, an expired code or invite, a used, revoked or locked invite and an email that got an account in the meantime all return the same response:

```json
{"error": {"code": "invalid_or_expired_code", "message": "The code is invalid or has expired."}}
```

`POST /auth/invite/request-code` (3 per hour per IP) with `{"email": "..."}` always returns 202 `{"message": "If an invitation exists for this address, a new code has been sent."}`. When a pending, unlocked, unexpired invite exists and the cooldown and send cap allow it, a new code replaces the old one (attempts are not reset) and the email is sent in a background task after the response.

`POST /auth/password-reset/request` (3 per hour per IP) with `{"email": "..."}` always returns 202 `{"message": "If an account with this email exists, a reset code has been sent."}`. It covers every enabled account, since every account has an email. At most `RESET_MAX_REQUESTS_PER_HOUR` (3) codes are issued per hour and not within `INVITE_RESEND_COOLDOWN_SECONDS` of the previous one; a new code supersedes the active one and is emailed in a background task.

`POST /auth/password-reset/confirm` (5 per minute per IP) with `{"email", "code", "new_password"}` returns 204. It uses the same two steps as accepting an invite (the attempt is committed first; the code locks after `RESET_MAX_ATTEMPTS` (5) wrong tries; the code is single use and expires after `RESET_CODE_TTL_MINUTES` (15)). On success the password is replaced, `must_change_password` is cleared and `password_changed_at` is set, so older tokens stop working. The user then signs in with the new password and, for the roles in `LOGIN_OTP_ROLES`, a sign in code. Every failure, including an unknown email and a disabled account, returns the same 400 `invalid_or_expired_code`. The password policy follows the account's role.
