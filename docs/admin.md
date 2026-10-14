# Admin runbook

How to operate the platform as an administrator. Endpoint details are in [api.md](api.md#admin-api).

## The main admin (super admin)

There is exactly one super admin. It is created once per environment from the command line, never through the API:

1. Set `SEED_ADMIN_NAME`, `SEED_ADMIN_PHONE`, `SEED_ADMIN_EMAIL` and, optionally, `SEED_ADMIN_PASSWORD` (at least 12 characters) in `.env`.
2. Run `python -m scripts.seed_admin`. Without `SEED_ADMIN_PASSWORD` it asks for the password twice.
3. Remove `SEED_ADMIN_PASSWORD` from `.env`, log in, and change the password when asked.

The database protects the super admin: it cannot be disabled, demoted, deleted, or have its password reset by another admin.

## Creating other admins

Only the super admin can create admins.

1. Call `POST /api/v1/admin/admins` with the new admin's name, phone number and email.
2. The response contains a `temporary_password`. It is shown only once and is never stored in plain text, so copy it immediately.
3. Give it to the new admin through a private channel (in person or a direct call), never by group chat or email.
4. The new admin logs in and must change the password before any other request works.

If a temporary password is lost, the super admin resets it with `POST /api/v1/admin/users/{id}/reset-password`.

## Disabling and enabling accounts

- `PATCH /api/v1/admin/users/{id}/disable` blocks the account: the user's next request returns 401 and login fails. Nothing is deleted.
- A disabled farmer's listings disappear from buyer browsing, detail and checkout, and come back when the farmer is enabled.
- `PATCH /api/v1/admin/users/{id}/enable` restores access.
- Any admin can disable farmers and buyers. Only the super admin can disable or enable another admin. Nobody can disable the super admin or themselves.

## Resetting a password

`POST /api/v1/admin/users/{id}/reset-password` returns a new temporary password once and forces a change at the next login. Existing access tokens keep working until they expire (60 minutes by default), because tokens carry no password version. To lock someone out immediately, disable the account instead.

## Removing a listing

`DELETE /api/v1/admin/listings/{id}` soft-removes a listing and cancels its unpaid orders, returning their stock. If a buyer is in the middle of paying (`listing_has_open_payments`), wait a few minutes until the payment finishes or the order expires, then try again. Paid orders are not affected.

## Orders that need reconciliation

An order becomes `needs_reconciliation` when M-Pesa reports a payment the platform could not simply accept. Find them with `GET /api/v1/admin/orders?status=needs_reconciliation`; `GET /api/v1/admin/stats` shows the count under `attention`. The `reconciliation` field says why:

| Reason | What happened |
| --- | --- |
| `late_success_after_expiry` | The buyer paid after the order expired; its stock had already been returned to the listing. |
| `success_on_closed_order` | A payment succeeded for an order that had been cancelled. |
| `success_on_closed_payment` | M-Pesa reported success for a payment the platform had already recorded as failed or cancelled. |
| `amount_mismatch` | M-Pesa confirmed a different amount from the order total. |

Before resolving, check the payment timeline (`GET /api/v1/admin/payments/{id}/events`) and, if possible, the buyer's M-Pesa confirmation message. Then call `POST /api/v1/admin/orders/{id}/resolve` with a note of at least 10 characters explaining the decision:

- **`confirm_sale`**: the money arrived and the sale stands. The order becomes `paid`. If the order's stock had already been returned (flagged after `expired` or `cancelled`), the quantity is taken from the listing again; if not enough is left, the request is refused (`insufficient_stock_to_confirm`) and you should void instead or agree a smaller delivery outside the platform.
- **`void_order`**: the sale is rejected. The order becomes `cancelled`. If its stock was still reserved (flagged while `pending_payment`), the quantity is returned to the listing.

A removed listing's stock is never changed. The payment record stays exactly as M-Pesa reported it, and the decision is stored as an `admin_action` event with your note.

**Refunds are outside the platform.** Payments run in the Daraja sandbox, so no real money moves, and the platform has no till or B2C payout. When a real deployment voids a paid sale, the refund must be arranged directly with the buyer and recorded in the resolution note.

## Reading the dashboard

`GET /api/v1/admin/stats`:

- `users`, `listings`, `orders`, `payments`: counts per role or status, with zeros shown.
- `completed_sandbox_amount_kes`: total of completed sandbox payments (simulated money).
- `attention`: orders waiting for reconciliation, and payments still `initiated` after 10 minutes (usually a missing callback; the buyer's next order view queries Daraja).
- `pipeline`: one row per market and crop. `latest_price_month` is the newest price from the ML pipeline; `forecast_origin_month` is the origin of the newest one-month forecast. `forecast_current: false` means the forecast is older than the latest prices, so buyers and farmers will not see it until the pipeline runs again. `last_prices_fetch` and `last_forecast_generated` show when the pipeline last wrote data.

## Reading forecast accuracy

`GET /api/v1/admin/forecast-accuracy` compares each forecast with the price that was later observed.

- Rows with `is_pending: true` have no actual price yet; their error columns are null and they are left out of the summary metrics (counted in `n_pending`).
- `summary` is split by horizon and by `is_backfill`. Backfilled forecasts were computed after the fact from the prices available at their origin month and were never shown live, so judge the live rows separately.
- `mape` is the mean absolute percentage error (lower is better). `directional_accuracy` is the share of forecasts that got the direction of the price move right. `beat_persistence_rate` is the share that beat the naive "next month equals this month" guess.
- Sample sizes are small, so treat the figures as indicative.
