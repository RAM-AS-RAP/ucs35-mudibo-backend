# Admin runbook

How to operate the platform as an administrator. Endpoint details are in [api.md](api.md#admin-api).

## The main admin (super admin)

There is exactly one super admin. It is created once per environment from the command line, never through the API:

1. Set `SEED_ADMIN_NAME`, `SEED_ADMIN_EMAIL` and, optionally, `SEED_ADMIN_PASSWORD` (at least 12 characters) in `.env`. There is no phone number.
2. Run `python -m scripts.seed_admin`. Without `SEED_ADMIN_PASSWORD` it asks for the password twice. The account gets a verified email and must change its password at the first sign in.
3. Remove `SEED_ADMIN_PASSWORD` from `.env`, sign in, and change the password when asked.

Every admin sign in has two steps: the password, then an 8-character code emailed to the admin's address (production always requires it for admins). So email delivery must work before the first sign in. Configure email (see [email.md](email.md)) and set `INVITE_CODE_PEPPER` (at least 32 random characters, for example from `python -c "import secrets; print(secrets.token_urlsafe(48))"`); the setting keeps its historical name but protects every email code. `POST /api/v1/admin/email/test` checks delivery, but it needs a signed in super admin, so for the very first sign in use `EMAIL_PROVIDER=console` (the code is written to `.dev_mailbox/` on the server's machine) or the recovery script below.

The database protects the super admin: it cannot be disabled, demoted or deleted. No admin can set or see another person's password.

## Recovery when email is down

`scripts/admin_recovery.py` is an operator tool for the case where an administrator cannot receive email. It refuses to run unless `ALLOW_ADMIN_RECOVERY=1` is set, works only for administrator accounts, and never takes a password or code on the command line. The operator already has database access, so it grants nothing new; use it only when needed and unset the variable afterwards.

- `python -m scripts.admin_recovery issue-login-code --email <admin email>`: for an enabled administrator, creates a sign in challenge exactly like `POST /auth/login` does (it supersedes the active one; the hourly challenge limit is not applied) and prints the challenge token and the code once to the terminal. Enter them with `POST /api/v1/auth/login/verify` (or hand them to the admin over a trusted channel). The code is valid for `LOGIN_CODE_TTL_MINUTES` (10).
- `python -m scripts.admin_recovery reset-password --email <admin email>`: asks twice for a new password (at least 12 characters, admin policy), stores its hash, sets `must_change_password`, and revokes every earlier token. The admin then signs in (which still needs a code) and must change the password.

On PowerShell set the variable with `$env:ALLOW_ADMIN_RECOVERY = "1"` and remove it with `Remove-Item Env:ALLOW_ADMIN_RECOVERY`.

## Inviting other admins

Only the super admin can invite admins. Nobody ever sees a password or a code in an API response.

**Super admin:**

1. Call `POST /api/v1/admin/invites` with the new admin's name and email address (invitations have no phone number).
2. The server emails an 8-character code (shown as `XXXX-XXXX`) to that address. The response only confirms it, with the masked email and `email_sent`.
3. Tell the person to expect the email (and to check the spam folder). The invite stays open for 7 days; each code works for 15 minutes.
4. Follow progress with `GET /api/v1/admin/invites` (`pending`, `used`, `expired`, `revoked`, `locked`).

If `email_sent` is `false`, fix the email configuration and call `POST /api/v1/admin/invites/{id}/resend`.

**Invited person:**

1. Open `<FRONTEND_BASE_URL>/accept-invite`.
2. Enter your email address, the code and a password of your own (at least 12 characters, not your email or name, and not a common password).
3. You are signed in straight away: the invite code proved that you control the email. No password change is forced, because you chose it yourself. Later sign ins use your email, the password and a code emailed to you.

### When a code expires, is lost, or the invite locks

- **The code expired (after 15 minutes) or the email was lost:** the invited person requests a new code on the accept page (`POST /api/v1/auth/invite/request-code`), or the super admin calls `POST /api/v1/admin/invites/{id}/resend`. The old code stops working. Resends wait 60 seconds between sends and stop after 5 sends per invite.
- **The invite expired (after 7 days), reached the send limit, or locked** (10 wrong codes in total; resending never resets this count): create a new invite with `POST /api/v1/admin/invites`. It replaces the old one automatically.
- **The wrong person was invited:** revoke the invite with `DELETE /api/v1/admin/invites/{id}`.

Every failed code entry gets the same "invalid or expired code" answer, whatever the reason, so an attacker learns nothing. Look at the invite's `status` and `attempts` in the invite list to see what happened.

## Disabling and enabling accounts

- `PATCH /api/v1/admin/users/{id}/disable` blocks the account: the user's next request returns 401 and login fails. Nothing is deleted.
- A disabled farmer's listings disappear from buyer browsing, detail and checkout, and come back when the farmer is enabled.
- `PATCH /api/v1/admin/users/{id}/enable` restores access.
- Any admin can disable farmers and buyers. Only the super admin can disable or enable another admin. Nobody can disable the super admin or themselves.

## Forgotten passwords

Admins (including the super admin), farmers and buyers reset their own passwords by email; no admin route resets a password.

1. Open `<FRONTEND_BASE_URL>/reset-password` and enter the account's email address. The answer is always the same, whether or not the address exists.
2. Enter the 8-character code from the email (valid for 15 minutes) and a new password. Admins need at least 12 characters, farmers and buyers at least 8.
3. Sign in again with the new password and the emailed sign in code. Every token issued before the change stops working at once, on all devices.

At most 3 codes are sent per account per hour, at least 60 seconds apart, and a code locks after 5 wrong tries; request a new one in that case. Disabled accounts get no code. Every account has a verified email, so every user can reset a forgotten password. To lock someone out immediately, disable the account. If an administrator cannot receive email at all, see [Recovery when email is down](#recovery-when-email-is-down).

Changing a password while signed in (`POST /api/v1/auth/change-password`) also revokes older tokens, including the one used for the request, so sign in again afterwards.

## Finding users

`GET /api/v1/admin/users?q=` searches names and email addresses (at least 2 characters; `%` and `_` are matched literally). Accounts have no phone number, so a number cannot be searched. Each item shows `email_verified_at`, when the address was confirmed.

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
