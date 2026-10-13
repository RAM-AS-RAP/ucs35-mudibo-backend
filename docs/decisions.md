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
