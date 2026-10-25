# Email setup

The backend sends five kinds of email: registration codes (valid 15 minutes), sign in codes (valid 10 minutes, sent after a correct password), "account exists" notices (no code; sent when someone registers with an address that already has an account), admin invitations and password reset codes (valid 15 minutes). Codes are 8 characters (shown as `XXXX-XXXX`). A code is never in the subject or in a link, no email contains a password or a link carrying a secret, and sends are never retried automatically.

**Email is a hard dependency of sign in.** With the default `LOGIN_OTP_ROLES` every farmer, buyer and admin needs an emailed code to register and to sign in, so a deployment whose email does not leave the host locks everyone out. Sign in codes are sent while the request waits (bounded by `SMTP_TIMEOUT_SECONDS` for every provider); registration, invite request and reset emails go out in background tasks after the response. If email is down, an operator can let an administrator in with `scripts/admin_recovery.py` (see [admin.md](admin.md)).

Choose one provider with `EMAIL_PROVIDER`:

| Provider | Transport | Use it for |
| --- | --- | --- |
| `console` | Writes files to `./.dev_mailbox/` | Local development only; refused when `APP_ENV=production`. |
| `smtp` | SMTP with TLS (for example Gmail) | Local machines and hosts that allow outbound SMTP. |
| `resend` | HTTPS API | Hosts that block SMTP, such as Render's free tier. |
| `brevo` | HTTPS API | Same as Resend; pick whichever account you can set up. |

## Why HTTPS APIs exist alongside SMTP

Many free hosting platforms block outbound SMTP ports. Render free web services, for example, block ports 25, 465 and 587, so SMTP (including Gmail) cannot work there. Resend and Brevo are called over HTTPS (port 443), which is never blocked. When `EMAIL_PROVIDER=smtp` and `APP_ENV=production`, the API logs one warning at startup about blocked ports; it does not refuse to start.

## Settings common to every provider

| Variable | Meaning |
| --- | --- |
| `EMAIL_PROVIDER` | `console` (default), `smtp`, `resend` or `brevo`. |
| `EMAIL_FROM_NAME` | Display name of the sender, default `Admin via AgriSence`. Recipients see "Admin via AgriSence" in their inbox. |
| `EMAIL_FROM_ADDRESS` | Sender address; required for `smtp`, `resend` and `brevo`. |
| `EMAIL_REPLY_TO` | Optional reply address. |
| `FRONTEND_BASE_URL` | Base URL of the web app; emails point to `<FRONTEND_BASE_URL>/accept-invite` and `/reset-password`. Must start with `https://` in production. |
| `INVITE_CODE_PEPPER` | Secret of at least 32 characters used to HMAC every email code (registration, sign in, invites, resets; the name is historical). Without it, every code route returns 503 `codes_unavailable`. |

To change the display name, set `EMAIL_FROM_NAME`, for example `EMAIL_FROM_NAME=AgriSence Support`. Line breaks are refused in every header value.

## Development: the console provider

With `EMAIL_PROVIDER=console` each message, including its code, is written to a file such as `.dev_mailbox/20261015T090000123456Z-1a2b3c4d.txt`. The log shows only `dev email written`. The folder is ignored by Git. Open the newest file to read the code.

## Gmail over SMTP

Use a dedicated Gmail account for the platform, not a personal one.

1. Sign in to the dedicated account and turn on **2-Step Verification** (Google Account, Security).
2. Create an **app password**: Google Account, Security, App passwords. The option appears only when 2-Step Verification is on. Some account types (for example some work, school or Advanced Protection accounts) do not allow app passwords; use another account or an HTTPS provider then.
3. Put the 16-character app password in `SMTP_PASSWORD`. Never use the account's normal password.
4. Set the other values:

```env
EMAIL_PROVIDER=smtp
EMAIL_FROM_NAME=Admin via AgriSence
EMAIL_FROM_ADDRESS=agrisence.admin@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=agrisence.admin@gmail.com
SMTP_PASSWORD=<the app password>
SMTP_TIMEOUT_SECONDS=15
```

`SMTP_USERNAME` and `EMAIL_FROM_ADDRESS` are both the Gmail address. Gmail replaces any other sender address with the account address, and the API logs a warning at startup when they differ. Port 587 uses `starttls`; port 465 uses `ssl` (the API warns when port and mode do not match). Certificate verification is always on and there is no setting to turn it off.

Gmail limits how many messages an account can send per day; check Google's current "Gmail sending limits" page before relying on it for many users.

## Resend

1. Create an account at resend.com.
2. **Verify a domain** you own (Domains, Add domain) and add the DNS records Resend shows (SPF/DKIM `TXT` and `MX` records) at your DNS provider. Wait until the domain shows as verified.
3. Create an **API key** with sending access (API Keys).
4. Configure:

```env
EMAIL_PROVIDER=resend
EMAIL_API_KEY=<the API key>
EMAIL_FROM_NAME=Admin via AgriSence
EMAIL_FROM_ADDRESS=admin@your-verified-domain.example
```

The request format used by the backend (`POST https://api.resend.com/emails` with a bearer key) should be re-checked against Resend's API reference when upgrading.

## Brevo

1. Create an account at brevo.com.
2. **Verify a sender** (Senders, Domains and Dedicated IPs, Senders: confirm the address by email) or, better, **authenticate a domain** by adding the DNS records Brevo shows.
3. Create an **API key** (SMTP and API, API keys). Use the API key, not the SMTP key.
4. Configure:

```env
EMAIL_PROVIDER=brevo
EMAIL_API_KEY=<the API key>
EMAIL_FROM_NAME=Admin via AgriSence
EMAIL_FROM_ADDRESS=<the verified sender address>
```

The backend calls `POST https://api.brevo.com/v3/smtp/email` with an `api-key` header; re-check it against Brevo's API reference when upgrading.

## Testing with a Gmail inbox

1. Start the API with the provider configured and log in as the super admin (whose email can be a Gmail address).
2. Call `POST /api/v1/admin/email/test` with an empty body. It sends a short message without any code to your own address and returns `{"email_sent": true}`, or 502 `email_failed` with a `category`:
   - `auth_failed`: wrong API key or SMTP app password.
   - `connection_timeout`: the server could not be reached (often a blocked SMTP port).
   - `tls_failed`: the secure connection failed (check `SMTP_PORT` and `SMTP_SECURITY`).
   - `recipient_refused`, `invalid_header`, `not_configured`, `other`.
3. Look in the inbox, then in **Spam** and the **Promotions** or **Updates** tabs. Mark the message "Not spam" so later codes arrive in the inbox. Unverified domains and new sending accounts are the usual cause of spam filing.
4. Invite yourself with a second address (for example a `+test` alias such as `you+invite@gmail.com`) to check the full flow.

## Hosting warning

Render free web services block outbound SMTP ports (25, 465 and 587). On such hosts:

- use an HTTPS provider (`resend` or `brevo`), or
- run the backend on a host that allows SMTP, or
- create the admins you need locally (with the API pointed at the same database) before deploying.

While SMTP is blocked, email-based password reset does not work on the deployed server.

## Rotating API keys and passwords

1. Create a new key (or a new Gmail app password) in the provider's dashboard.
2. Replace `EMAIL_API_KEY` (or `SMTP_PASSWORD`) in the server's environment and restart the API.
3. Send a test with `POST /api/v1/admin/email/test`.
4. Delete the old key or app password in the provider's dashboard.

Rotate immediately if a key may have leaked. Keys and passwords are never logged or returned by the API. Rotating `INVITE_CODE_PEPPER` invalidates every outstanding registration, sign in, invite and reset code (users request new ones).
