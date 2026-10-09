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
| GET | `/markets` | Any authenticated user | None | Market directory. |
| GET | `/prices/current` | Any authenticated user | `crop` query. | Latest price and month per market. |
| GET | `/prices/trends` | Any authenticated user | `market`, `crop`, optional `months` (1-60). | Ordered historical prices. |
| GET | `/forecasts` | Farmer | `market`, `crop`, `horizon` queries. | Latest forecast and unchanged SHAP values; 404 if unavailable. |
| POST | `/listings` | Farmer | Multipart crop, market, quantity, unit, price, optional image. | Created listing. |
| GET | `/listings` | Any authenticated user | Optional crop/market, limit, offset. | Active listings with stock. |
| GET | `/listings/{id}` | Any authenticated user | Listing UUID. | Listing details. |
| GET | `/listings/mine` | Farmer | None | Farmer's listings. |
| PATCH | `/listings/{id}` | Owning farmer | Editable stock, price, unit, optional image. | Updated listing. |
| DELETE | `/listings/{id}` | Owning farmer | Listing UUID. | Soft-removal confirmation. |
| GET | `/listings/mine/orders` | Farmer | None | Orders against the farmer's listings. |
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

Services marked TODO require database migrations and schema constraints before their behavior is enabled.

## Authentication details

`POST /auth/register/farmer` and `POST /auth/register/buyer` accept a JSON body with `name`, `phone_number`, optional `email`, and `password`. Unknown fields are rejected. The role is assigned by the route.

Registration passwords and `new_password` on `/auth/change-password` must be 8-128 characters and include at least one uppercase letter, one lowercase letter, one digit, and one non-whitespace symbol. Login accepts existing passwords without reapplying the creation policy.

```json
{
	"name": "Amina Farmer",
	"phone_number": "0712345678",
	"email": "amina@example.com",
	"password": "a-long-private-password"
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