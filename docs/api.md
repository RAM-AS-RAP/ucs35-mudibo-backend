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