# API endpoints

All paths are prefixed with `/api/v1`. Authenticated routes require a bearer access token. `farmer`, `buyer`, and `admin` describe allowed roles; super-admin routes require an admin account with the super-admin flag.

| Method | Path | Allowed roles | Request | Response |
| --- | --- | --- | --- | --- |
| GET | `/health` | Public | None | API health status. |
| POST | `/auth/register/farmer` | Public | Name, phone, optional email, password. | Created farmer account. |
| POST | `/auth/register/buyer` | Public | Name, phone, optional email, password. | Created buyer account. |
| POST | `/auth/login` | Public | Phone/email identifier and password. | Bearer token, role, forced-password-change flag. |
| POST | `/auth/change-password` | Any authenticated user | Current and new password. | Updated account. |
| GET | `/auth/me` | Any authenticated user | None | Current account. |
| GET | `/markets` | Farmer, admin | None | Markets with price data, with each market's latest observed month. |
| GET | `/prices/current` | Farmer, admin | `crop=maize` or `crop=beans`. | `{items, as_of_note}`; each item has current price/source/fetched time and the immediately previous calendar-month price/change, null when missing. Empty items if that crop has no data. |
| GET | `/prices/trends` | Farmer, admin | Available `market`, `crop`, `months` (1-60, default 24). | Last `months` calendar months in ascending order; missing months are absent. 404 `market_not_available` if no price data exists for that market. |
| GET | `/forecasts` | Farmer, admin | Available `market`, `crop`, `horizon=one_month` or `two_month`. | Stored forecast with `expected_change_pct`, unchanged SHAP values, `based_on`, and `for_month`; 404 if absent or stale. |
| POST | `/listings` | Farmer | Multipart crop, market, quantity, `unit=kg`, price, optional JPEG/PNG image. | Created `ListingOut` (201). Errors: `market_not_available` (404), `listing_limit_reached` (409), `invalid_image` (422), `image_too_large` (413). |
| GET | `/listings/mine` | Farmer | Optional `status`, `limit` (1-100, default 20), `offset`. | Own listings, newest first; removed listings are included by default. |
| PATCH | `/listings/{id}` | Owning farmer | Multipart optional quantity, price, image replacement, or `remove_image`. | Updated `ListingOut`. Errors: `listing_not_found` (404), `listing_removed` (409), `no_changes` (422). |
| DELETE | `/listings/{id}` | Owning farmer | Listing UUID. | Soft-remove with 204; repeat delete is idempotent. `listing_has_pending_orders` returns 409. |
| GET | `/listings/mine/orders` | Farmer | Optional order `status`, `listing_id`, `limit` (1-100, default 20), `offset`. | Paginated own-listing orders with latest payment metadata, buyer name only, and a sandbox/delivery note. |
| POST | `/orders` | Buyer | Listing UUID and quantity. | Created order with server-calculated total. |
| GET | `/orders/mine` | Buyer | None | Purchase history, newest first. |
| GET | `/orders/{id}` | Owning buyer or admin | Order UUID. | Order and latest payment; may reconcile stale payment. |
| POST | `/orders/{id}/pay` | Owning buyer | `Idempotency-Key` header. | Payment status. |
| POST | `/payments/callback/{token}` | Daraja callback | Daraja STK callback JSON. | Always accepts valid-token callbacks with ResultCode 0. |
| POST | `/admin/admins` | Super admin | Name, phone, email, temporary password. | Created non-super admin. |
| GET | `/admin/users` | Admin | Optional role, limit, offset. | Paginated users. |
| PATCH | `/admin/users/{id}/disable` | Admin | User UUID. | Disabled account. |
| PATCH | `/admin/users/{id}/enable` | Admin | User UUID. | Enabled account. |
| GET | `/admin/listings` | Admin | Limit, offset. | Paginated listings. |
| DELETE | `/admin/listings/{id}` | Admin | Listing UUID. | Soft-removal confirmation. |
| GET | `/admin/payments` | Admin | Limit, offset. | Paginated payments and order references. |
| GET | `/admin/stats` | Admin | None | User, listing, order, and sandbox payment aggregates. |
| GET | `/admin/forecast-accuracy` | Admin | Optional market, crop, horizon. | Accuracy rows and pooled summary. |

Admin services remain placeholders and return `501 not_implemented`. Buyer listing browse/detail routes are out of scope and are not registered. Buyers cannot access markets, prices, forecasts, or farmer listings; only farmers and admins can read market data.

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

## Authentication details

`POST /auth/register/farmer` and `POST /auth/register/buyer` accept a JSON body with `name`, `phone_number`, optional `email`, and `password`. Unknown fields are rejected. The role is assigned by the route.

Registration passwords and `new_password` on `/auth/change-password` must be 8-128 characters and include at least one uppercase letter, one lowercase letter, one digit, and one non-whitespace symbol. Login accepts existing passwords without reapplying the creation policy.

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

`POST /auth/change-password` accepts `current_password` and `new_password`, returns `204`, and rejects a new password equal to the current password. `GET /auth/me` returns the current user projection.

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

The response describes failed rules but never includes the submitted password. Authentication error codes: `invalid_credentials` (`401`) is identical for unknown, incorrect-password, and disabled accounts; `password_change_required` (`403`) blocks other authenticated routes until the temporary password changes; `forbidden` (`403`) indicates a role or super-admin failure; `rate_limited` (`429`) covers IP and per-identifier throttles; `conflict` (`409`) is the generic duplicate-account response; `validation_error` (`422`) lists field names and safe validation details.