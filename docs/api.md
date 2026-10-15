# API endpoints

All paths are prefixed with `/api/v1`. Authenticated routes require a bearer access token. `farmer`, `buyer`, and `admin` describe allowed roles; super-admin routes require an admin account with the super-admin flag.

| Method | Path | Allowed roles | Request | Response |
| --- | --- | --- | --- | --- |
| GET | `/health` | Public | None | API health status. |
| POST | `/auth/register/farmer` | Public | Name, phone, optional email, password. | Created farmer account. |
| POST | `/auth/register/buyer` | Public | Name, phone, optional email, password. | Created buyer account. |
| POST | `/auth/login` | Public | Phone/email identifier and password. | Bearer token, role, forced-password-change flag. |
| POST | `/auth/change-password` | Any authenticated user | Current and new password. | 200 with a note that the user must log in again; earlier tokens are revoked. |
| GET | `/auth/me` | Any authenticated user | None | Current account. |
| POST | `/auth/accept-invite` | Public | JSON `email`, `code`, `password`. | 201 `UserOut` of the new admin. |
| POST | `/auth/invite/request-code` | Public | JSON `email`. | Always 202 with the same body. |
| POST | `/auth/password-reset/request` | Public | JSON `email`. | Always 202 with the same body. |
| POST | `/auth/password-reset/confirm` | Public | JSON `email`, `code`, `new_password`. | 204. |
| GET | `/markets` | Farmer, admin | None | Markets with price data, with each market's latest observed month. |
| GET | `/prices/current` | Farmer, admin | `crop=maize` or `crop=beans`. | `{items, as_of_note}`; each item has current price/source/fetched time and the immediately previous calendar-month price/change, null when missing. Empty items if that crop has no data. |
| GET | `/prices/trends` | Farmer, admin | Available `market`, `crop`, `months` (1-60, default 24). | Last `months` calendar months in ascending order; missing months are absent. 404 `market_not_available` if no price data exists for that market. |
| GET | `/forecasts` | Farmer, admin | Available `market`, `crop`, `horizon=one_month` or `two_month`. | Stored forecast with `expected_change_pct`, unchanged SHAP values, `based_on`, and `for_month`; 404 if absent or stale. |
| POST | `/listings` | Farmer | Multipart crop, market, quantity, `unit=kg`, price, optional JPEG/PNG image. | Created `ListingOut` (201). Errors: `market_not_available` (404), `listing_limit_reached` (409), `invalid_image` (422), `image_too_large` (413). |
| GET | `/listings/mine` | Farmer | Optional `status`, `limit` (1-100, default 20), `offset`. | Own listings, newest first; removed listings are included by default. |
| PATCH | `/listings/{id}` | Owning farmer | Multipart optional quantity, price, image replacement, or `remove_image`. | Updated `ListingOut`. Errors: `listing_not_found` (404), `listing_removed` (409), `no_changes` (422). |
| DELETE | `/listings/{id}` | Owning farmer | Listing UUID. | Soft-remove with 204; repeat delete is idempotent. `listing_has_pending_orders` returns 409. |
| GET | `/listings/mine/orders` | Farmer | Optional order `status`, `listing_id`, `limit` (1-100, default 20), `offset`. | Paginated own-listing orders with latest payment metadata, buyer name only, and a sandbox/delivery note. |
| GET | `/listings` | Buyer | Optional `crop`, `market`, `min_price`, `max_price`, `sort`, `limit`, `offset`. | Paginated active listings with stock (`PublicListingOut`). |
| GET | `/listings/{id}` | Buyer | Listing UUID. | Active or sold-out listing with `available`; removed or unknown is 404. |
| POST | `/orders` | Buyer | JSON `listing_id` and `quantity`. | Created `OrderOut` (201) with stock reserved and a server-calculated total. |
| GET | `/orders/mine` | Buyer | Optional `status`, `limit`, `offset`. | Own orders, newest first, with listing and payment summaries. |
| GET | `/orders/{id}` | Owning buyer, or admin (read only) | Order UUID. | `OrderOut`. |
| POST | `/orders/{id}/cancel` | Owning buyer | Order UUID. | Cancelled `OrderOut`; stock is returned to the listing. |
| POST | `/orders/{id}/pay` | Owning buyer | `Idempotency-Key` header, empty body. | 202 `OrderOut` plus `message` and `prompt`; an M-Pesa sandbox STK prompt is sent. |
| POST | `/payments/callback/{token}` | Daraja only (no authentication) | Daraja STK callback JSON. | 404 with an empty body for a wrong token; otherwise always 200 `{"ResultCode": 0, "ResultDesc": "Accepted"}`. |
| POST | `/admin/invites` | Super admin | JSON `name`, `phone_number`, `email`. | 201 invite summary; the code is emailed, never returned. |
| POST | `/admin/invites/{id}/resend` | Super admin | Empty body. | Invite summary; a new code is emailed. |
| GET | `/admin/invites` | Super admin | Optional `status`, `limit`, `offset`. | Paginated invites. |
| DELETE | `/admin/invites/{id}` | Super admin | Invite UUID. | 204; revokes a pending invite. |
| POST | `/admin/email/test` | Super admin | Empty body. | `{"email_sent": true}` or 502 `email_failed` with a `category`. |
| GET | `/admin/users` | Admin | Optional `role`, `is_disabled`, `q`, `limit`, `offset`. | Paginated `UserAdminOut`, newest first. |
| PATCH | `/admin/users/{id}/disable` | Admin (super admin for admin targets) | Empty body. | `UserAdminOut`; idempotent. |
| PATCH | `/admin/users/{id}/enable` | Admin (super admin for admin targets) | Empty body. | `UserAdminOut`; idempotent. |
| GET | `/admin/listings` | Admin | Optional `status`, `crop`, `market`, `farmer_id`, `q`, `limit`, `offset`. | Paginated `AdminListingOut`. |
| DELETE | `/admin/listings/{id}` | Admin | Listing UUID. | 204; soft removal that cancels unpaid orders. |
| GET | `/admin/orders` | Admin | Optional `status`, `q`, `limit`, `offset`. | Paginated `AdminOrderOut`. |
| GET | `/admin/orders/{id}` | Admin | Order UUID. | `AdminOrderOut` with `payments` and `resolution`. |
| POST | `/admin/orders/{id}/resolve` | Admin | JSON `action`, `note`. | Resolved order. |
| GET | `/admin/payments` | Admin | Optional `status`, `order_reference`, `environment`, `created_from`, `created_to`, `limit`, `offset`. | Paginated `AdminPaymentOut`. |
| GET | `/admin/payments/{id}/events` | Admin | Payment UUID. | Event timeline, oldest first, at most 100. |
| GET | `/admin/stats` | Admin | None | `StatsOut` dashboard counts and pipeline status. |
| GET | `/admin/forecast-accuracy` | Admin | Optional `market`, `crop`, `horizon`, `is_backfill`, `include_pending`, `limit`, `offset`. | `AccuracyOut` rows and summary. |

Admin routes are described in [Admin API](#admin-api) and operated with [admin.md](admin.md). Payments run only against the Safaricom Daraja sandbox (see [payments.md](payments.md)). Buyers cannot see prices or forecasts and cannot access markets or farmer listing routes; only farmers and admins can read market data.

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

Errors use `{"error":{"code":"...","message":"..."}}`; forecast errors may also include `reason`.

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
| `GET /listings/{id}` | Buyer | 200 | 404 `listing_not_found` (removed or unknown, identical bodies); 422 for a non-UUID id. |
| `POST /orders` | Buyer | 201 | 404 `listing_not_found`; 409 `listing_unavailable`, `insufficient_stock`, `pending_order_exists` (with `existing_order_id`), `too_many_pending_orders`; 422 `order_too_small` or `validation_error`; 429 `rate_limited` (30 per hour per IP). |
| `GET /orders/mine` | Buyer | 200 | 422 `validation_error` for an invalid status or pagination. |
| `GET /orders/{id}` | Owning buyer, admin | 200 | 404 `order_not_found` (another buyer's order and an unknown id give identical bodies). |
| `POST /orders/{id}/cancel` | Owning buyer | 200 (also for an already cancelled order) | 404 `order_not_found`; 409 `order_not_cancellable` (paid, expired, or any other non-pending status), `payment_in_progress`; 429 `rate_limited` (30 per hour per IP). |
| `POST /orders/{id}/pay` | Owning buyer | 202 (also for a replay of the same key) | 404 `order_not_found`; 409 `order_not_payable`, `order_expiring` (`reason` `expired` or `too_close`), `payment_in_progress`, `max_attempts_reached`; 422 `idempotency_key_required`, `invalid_idempotency_key`, `body_not_allowed`; 429 `too_many_payment_attempts` or `rate_limited` (10 per minute per IP); 502 `payment_request_failed`; 503 `payments_unavailable`. |

All routes return 401 without a valid token and 403 `forbidden` for other roles. Farmers and admins get 403 on `GET /listings` and `GET /listings/{id}`; buyers get 403 on every farmer listing route.

### `GET /listings`

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

### `GET /listings/{id}`

Returns the same fields plus `available` (true when the listing is active and has stock). A `sold_out` listing is returned with `available: false`.

### `POST /orders`

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
- `payment` is null, or the latest payment: `status`, `status_message`, `stk_target_masked` (for example `254*******149`), `mpesa_receipt_number`, `result_desc`, `completed_at`, and `attempts` (number of payment rows). `status_message` is a fixed English text per status: `initiating` "Sending the payment request", `initiated` "Waiting for you to confirm on your phone", `completed` "Payment received", `cancelled` "You cancelled the payment request", `timeout` "The request timed out", `failed` "The payment failed". Payment identifiers, idempotency keys, checkout and merchant request ids, full phone numbers, and callback data are never returned.
- `can_pay` is true only for the owning buyer, when the order is `pending_payment`, not expired, has at least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` (default 90) seconds left on the database clock, has no open (`initiating` or `initiated`) payment, and has fewer than `MPESA_MAX_ATTEMPTS_PER_ORDER` (default 3) payment rows. `can_cancel` is true for the owning buyer when the order is `pending_payment` with no open payment. Both are false for an admin.
- When the owning buyer reads `GET /orders/{id}`, the latest payment is reconciled first: a stale `initiating` payment is failed, and an `initiated` payment older than `RECONCILE_AFTER_SECONDS` (or a `timeout` payment younger than `MPESA_TIMEOUT_RECONCILE_WINDOW_MINUTES`) is checked with an STK Push Query, at most once every `MPESA_QUERY_MIN_INTERVAL_SECONDS`. A reconciliation failure never fails the request. Admin reads never reconcile.

### `GET /orders/mine`

Query parameters: optional `status` (an order status), `limit` (1-100, default 20), `offset`. Returns `{items, total, limit, offset}` of `OrderOut`, newest first.

### `POST /orders/{id}/cancel`

Cancels the buyer's own `pending_payment` order and returns its quantity to the listing; a `sold_out` listing becomes `active` again (a removed listing gets its quantity back but stays removed). Cancelling an already cancelled order returns 200 with the current order.

### `POST /orders/{id}/pay`

Sends an M-Pesa STK Push prompt for the order through the Safaricom Daraja **sandbox** (shortcode 174379). No real money moves; a `paid` order is a simulated confirmation.

Headers: `Authorization: Bearer <token>` and `Idempotency-Key` (required, 8 to 64 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`). The body must be empty. The client never sends an amount, phone number or order data: the amount is the order total and the phone is the buyer's registered number (or the sandbox test number when `MPESA_STK_TARGET=test_msisdn`). Rate limit: 10 per minute per IP.

The key is stored scoped to the buyer and order, so it cannot collide with or probe other buyers' keys. Repeating a request with the same key returns 202 with the current order and never sends a second prompt, even after the order has moved on. Use a new key for a new attempt.

The payment can start only when the order is `pending_payment`, at least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` (default 90) seconds remain before `expires_at`, no other payment is open, the order has fewer than `MPESA_MAX_ATTEMPTS_PER_ORDER` (default 3) attempts, and the prompt phone has fewer than `MPESA_PHONE_ATTEMPTS_PER_HOUR` (default 5) attempts in the last hour. An `initiating` payment whose request was never sent (older than `MPESA_STALE_INITIATING_SECONDS`, default 60) is failed and does not block a new attempt.

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

(Other `OrderOut` fields are omitted above.) Then poll `GET /orders/{id}` until the payment is final.

Errors:

| Status | Code | When |
| --- | --- | --- |
| 404 | `order_not_found` | Unknown order or another buyer's order (identical bodies). |
| 409 | `order_not_payable` | The order is not `pending_payment`. |
| 409 | `order_expiring` | Fewer than 90 seconds remain; `reason` is `expired` or `too_close`. |
| 409 | `payment_in_progress` | Another payment for the order is `initiating` or `initiated`. |
| 409 | `max_attempts_reached` | The order already has the maximum number of payment attempts. |
| 422 | `idempotency_key_required` / `invalid_idempotency_key` | Missing or malformed `Idempotency-Key`. |
| 422 | `body_not_allowed` | The request had a body. |
| 429 | `too_many_payment_attempts` | Too many prompts to the same phone in the last hour. |
| 429 | `rate_limited` | More than 10 requests per minute from one IP. |
| 502 | `payment_request_failed` | Daraja rejected the request or did not answer in time ("Could not start the M-Pesa prompt. Please try again."). After a timeout the prompt may still have arrived, so a duplicate prompt is possible in the sandbox. |
| 503 | `payments_unavailable` | Payment settings are missing or `MPESA_CALLBACK_BASE_URL` is not HTTPS. |

### `POST /payments/callback/{token}`

Called by Daraja only, never by the PWA. There is no bearer authentication: the secret `MPESA_CALLBACK_TOKEN` in the path is the check, and a wrong token returns 404 with an empty body. Bodies over `CALLBACK_MAX_BYTES` (default 16384) return 413. After a valid token the endpoint always returns 200 with `{"ResultCode": 0, "ResultDesc": "Accepted"}`, also for malformed, unknown, duplicate or mismatched callbacks, so Daraja never retries. Every callback is stored as a payment event first. The phone number in the callback is ignored. There is no rate limit on this route. The server access log shows the path as `/api/v1/payments/callback/***`.

## Admin API

All routes are under `/api/v1/admin` and require an admin account; the role is read from the database on every request. Every route returns 401 without a valid token, 403 `forbidden` for farmers and buyers, and 403 `password_change_required` until the account's password has been changed (the seeded super admin's first login). Pagination uses `limit` (1-100, default 20) and `offset`, and returns `{items, total, limit, offset}`. Search `q` is 2 to 50 characters; `%` and `_` match literally.

Rate limits per IP: the four invite routes 10 per hour each; `POST /admin/email/test` 5 per hour; disable, enable, listing removal and order resolution 30 per hour (429 `rate_limited`).

No route ever returns a credential. New admins are invited by an emailed code and choose their own password; anyone who forgets a password resets it by email (see [Invites and password reset](#invites-and-password-reset)). Admin responses never contain password hashes, codes or code hashes, idempotency keys, merchant request ids, Daraja credentials or full payment phone numbers (payment targets are masked, for example `254*******149`). Users' own phone numbers and emails appear only in the user routes; invite routes show masked emails (`g***@e***.com`) and phone numbers.

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
{"name": "Grace Admin", "phone_number": "0712000001", "email": "grace@example.com"}
```

The phone number is normalised and the email lowercased. In one transaction the server:

1. Refuses with 409 `user_exists` when an account already has this phone number or email.
2. Revokes any earlier pending invite for the same email or phone, so the newest invite replaces it.
3. Refuses with 409 `too_many_pending_invites` when `INVITE_MAX_PENDING` (10) invites are pending.
4. Stores the invite with an HMAC of a new 8-character code, `expires_at = now() + INVITE_TTL_DAYS` (7 days) and `code_expires_at = now() + INVITE_CODE_TTL_MINUTES` (15 minutes), using the database clock.

After the commit the code is emailed to the invited address. The response (`Cache-Control: no-store`) never contains the code:

```json
{
	"id": "6f1c0e8a-2b44-4c55-9a1e-6c2f8d0b7e11",
	"name": "Grace Admin",
	"email_masked": "g***@e***.com",
	"phone_masked": "254*******001",
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

Categories: `connection_timeout`, `auth_failed`, `tls_failed`, `recipient_refused`, `invalid_header`, `not_configured`, `other`. When `INVITE_CODE_PEPPER` is not set, the invite create and resend routes and the public code routes return 503 `codes_unavailable`; the rest of the API is unaffected.

### Users

`GET /admin/users` filters by `role`, `is_disabled` and `q` (case-insensitive name or email, or a phone-number prefix such as `0712`). Items are `UserAdminOut`:

```json
{
	"id": "6f1c0e8a-2b44-4c55-9a1e-6c2f8d0b7e11",
	"name": "Grace Admin",
	"phone_number": "254712000001",
	"email": "grace@example.com",
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

`GET /admin/orders/{id}` adds `payments` (each `id, status, amount, stk_target_masked, mpesa_receipt_number, result_code, result_desc, environment, created_at, completed_at, callback_received_at`) and `resolution` (`{action, note, admin_name, created_at}` from the latest `admin_action` event) when one exists.

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

`GET /admin/payments` filters by `status`, `order_reference` (exact), `environment` (`sandbox` or `production`), and `created_from` / `created_to` (dates in Africa/Nairobi). Items: `id, order_id, order_reference, order_status, buyer_name, amount, status, stk_target_masked, mpesa_receipt_number, result_code, result_desc, environment, created_at, completed_at, callback_received_at, attempt_number`.

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

`POST /auth/register/farmer` and `POST /auth/register/buyer` accept a JSON body with `name`, `phone_number`, optional `email`, and `password`. Unknown fields are rejected. The role is assigned by the route.

Registration passwords and `new_password` on `/auth/change-password` must be 8-128 characters and include at least one uppercase letter, one lowercase letter, one digit, and one non-whitespace symbol. Login accepts existing passwords without reapplying the creation policy.

New passwords chosen through change-password, invite acceptance and password reset also pass the password policy (`app/core/password_policy.py`): admins need at least 12 characters, farmers and buyers at least 8, everyone at most 128; the password may not equal the account's email, the part of the email before `@`, the name or the phone number (ignoring case), and may not be one of the common passwords in `app/core/common_passwords.txt`. A failure returns 422 with a short reason that never repeats the password:

```json
{"error": {"code": "password_too_weak", "message": "The password is too weak.", "reason": "Use at least 12 characters."}}
```

```json
{
	"name": "Amina Farmer",
	"phone_number": "0712345678",
	"email": "amina@example.com",
	"password": "A-long-private-passw0rd"
}
```

Successful registration returns `201` and a user projection containing `id`, `name`, normalized `phone_number`, `email`, `role`, `must_change_password`, and `created_at`. It never includes a password hash or disabled-account details.

`POST /auth/login` accepts `identifier` (email or Kenyan phone number) and `password`. A successful response is `200`:

```json
{
	"access_token": "<signed-token>",
	"token_type": "bearer",
	"expires_in": 3600,
	"role": "farmer",
	"must_change_password": false
}
```

`POST /auth/change-password` accepts `current_password` and `new_password`, rejects a new password equal to the current password, and returns `200`:

```json
{"message": "Password changed. Log in again: your current access token is no longer accepted.", "login_required": true}
```

`GET /auth/me` returns the current user projection.

Access tokens carry their issue time (`iat`, whole seconds). Every password change (change-password, invite acceptance, password reset, and the seed script) sets `users.password_changed_at` from the database clock, and a token whose `iat` is earlier than the whole second of `password_changed_at` gets the same 401 as any invalid token. A token issued in the same second as the change stays valid.

For example, a password missing an uppercase letter and symbol returns `422` with safe field-specific details:

```json
{
	"error": {
		"code": "validation_error",
		"message": "Request validation failed",
		"fields": ["password"],
		"details": [
			{
				"field": "password",
				"message": "Value error, Password must contain an uppercase letter; a symbol"
			}
		]
	}
}
```

The response describes failed rules but never includes the submitted password. Authentication error codes: `invalid_credentials` (`401`) is identical for unknown, incorrect-password, and disabled accounts; `password_change_required` (`403`) blocks other authenticated routes until a forced password change is done (the seeded super admin's first login); `forbidden` (`403`) indicates a role or super-admin failure; `rate_limited` (`429`) covers IP and per-identifier throttles; `conflict` (`409`) is the generic duplicate-account response; `validation_error` (`422`) lists field names and safe validation details.
### Invites and password reset

These public routes need no token. Codes are 8 characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789`, shown in emails as `XXXX-XXXX`; spaces, hyphens and lower case are accepted. A code is never put in a URL, a subject line, a response or a log. When `INVITE_CODE_PEPPER` is not set, all four return 503 `codes_unavailable`.

`POST /auth/accept-invite` (5 per minute per IP). Body, unknown fields rejected:

```json
{"email": "grace@example.com", "code": "K7QX-2MPA", "password": "A-long-private-passw0rd"}
```

The password policy is checked first (admin rules) and a failure returns 422 `password_too_weak` that reveals nothing about any invite. The attempt is then counted in its own committed transaction, so a failed guess always counts; after `INVITE_MAX_ATTEMPTS` (10) wrong codes in total, across all resends, the invite is locked and even the correct code is refused. A correct code claims the invite and creates the admin in one transaction: `role=admin`, `is_super_admin=false`, `must_change_password=false`, `created_by` = the inviting super admin. The response is 201 with the user projection, and the new admin logs in normally. An unknown email, a wrong code, an expired code or invite, a used, revoked or locked invite and a duplicate phone or email all return the same response:

```json
{"error": {"code": "invalid_or_expired_code", "message": "The code is invalid or has expired."}}
```

`POST /auth/invite/request-code` (3 per hour per IP) with `{"email": "..."}` always returns 202 `{"message": "If an invitation exists for this address, a new code has been sent."}`. When a pending, unlocked, unexpired invite exists and the cooldown and send cap allow it, a new code replaces the old one (attempts are not reset) and the email is sent in a background task after the response.

`POST /auth/password-reset/request` (3 per hour per IP) with `{"email": "..."}` always returns 202 `{"message": "If an account with this email exists, a reset code has been sent."}`. For an enabled account with that email, at most `RESET_MAX_REQUESTS_PER_HOUR` (3) codes are issued per hour and not within `INVITE_RESEND_COOLDOWN_SECONDS` of the previous one; a new code supersedes the active one and is emailed in a background task. Farmers and buyers who registered without an email cannot use this flow.

`POST /auth/password-reset/confirm` (5 per minute per IP) with `{"email", "code", "new_password"}` returns 204. It uses the same two steps as accepting an invite (the attempt is committed first; the code locks after `RESET_MAX_ATTEMPTS` (5) wrong tries; the code is single use and expires after `RESET_CODE_TTL_MINUTES` (15)). On success the password is replaced, `must_change_password` is cleared and `password_changed_at` is set, so older tokens stop working. Every failure, including an unknown email and a disabled account, returns the same 400 `invalid_or_expired_code`. The password policy follows the account's role.
