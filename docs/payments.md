# M-Pesa payments (Daraja sandbox)

Payments use the Safaricom Daraja **sandbox** only (shortcode 174379, `https://sandbox.safaricom.co.ke`). No real money moves, the platform has no till, and a `paid` order is a simulated confirmation. There is no production setting: the configuration and the client constructor both reject any other environment, URL or shortcode. There is no B2C or payout to farmers.

Code: `app/integrations/daraja.py` (client), `app/services/payment_service.py` (pay flow, results, callbacks, reconciliation), `app/routers/orders.py` (`POST /orders/{id}/pay`), `app/routers/payments.py` (callback).

## Payment states

| From | Allowed next states | Set by |
| --- | --- | --- |
| (insert) | `initiating` | Pay endpoint, phase 1. The insert trigger requires a `pending_payment` order and `amount = orders.total_amount`. |
| `initiating` | `initiated` | Daraja accepted the STK request (ids saved). |
| `initiating` | `failed` | Daraja rejected the request or timed out (`request_rejected`, `request_unconfirmed`), or the request was never sent (`request_not_sent`, after `MPESA_STALE_INITIATING_SECONDS`). |
| `initiating` | `timeout` | The expiry job, when the order expires. |
| `initiated` | `completed` | Success callback or query with a matching amount. |
| `initiated` | `failed` | Failure result, or a success callback with a different amount (`amount_mismatch`). |
| `initiated` | `cancelled` | Result code 1032 (the customer cancelled). |
| `initiated` | `timeout` | Result code 1037, or the expiry job. |
| `timeout` | `completed` | A late success (callback or query). |
| `completed`, `failed`, `cancelled` | none | Terminal; the database rejects any update. |

There is no transition from `initiating` straight to `completed`. Only one payment per order can be `initiating` or `initiated` (partial unique index). A receipt number can belong to only one payment.

## Effects on the order

| Result | Order before | Order after |
| --- | --- | --- |
| Success | `pending_payment` | `paid` |
| Success | `expired`, `cancelled`, `payment_failed` | `needs_reconciliation` (stock was already restored; an administrator must resolve it) |
| Success | `paid`, `needs_reconciliation` | unchanged |
| Success on a `failed` or `cancelled` payment | anything except `paid` | `needs_reconciliation`, plus an `error` event |
| Amount mismatch | anything except `paid` | `needs_reconciliation`, plus an `error` event; never `paid` |
| Cancelled, timeout, failed | any | unchanged; the buyer can try again until the order expires. Stock is not touched. |

A `paid` order can never change status (database trigger). The order status `payment_failed` stays unused.

## Pay flow (`POST /orders/{id}/pay`)

Phase 1, one short transaction with no HTTP call:

1. Check the payment settings (else 503 `payments_unavailable`).
2. Look up the stored key `"{buyer_id}:{order_id}:{Idempotency-Key}"`. If found, return 202 with the current order (an exact replay; Daraja is not called).
3. Lock the buyer's order `FOR UPDATE` (else 404).
4. Require `pending_payment` (else 409 `order_not_payable`).
5. Require at least `MPESA_MIN_SECONDS_BEFORE_EXPIRY` seconds left on the database clock (else 409 `order_expiring`).
6. Fail a stale `initiating` payment without a checkout id; any other open payment gives 409 `payment_in_progress`.
7. Check attempts per order and prompts per phone in the last hour.
8. Insert the payment as `initiating` with the order total and the server-chosen phone, then commit. This releases the order lock before the network call.

Phase 2, no transaction open:

9. Log a redacted `stk_request` event.
10. Call STK Push once. It is never retried automatically.
11. Accepted: lock the order, then the payment, save `checkout_request_id` and `merchant_request_id`, set `initiated`, commit, log `stk_response`, then apply any callback that arrived early (`rematch_orphan_events`). Return 202.
12. Rejected, failed or timed out: set the payment `failed` (`request_rejected` or `request_unconfirmed`), log an `error` event, return 502 `payment_request_failed`. After a timeout the prompt may still have been sent, so a duplicate prompt is possible in the sandbox.
13. If the process dies between the phases, the payment stays `initiating`. Reconciliation fails it after `MPESA_STALE_INITIATING_SECONDS`, or the expiry job times it out with the order.

## Callback flow (`POST /payments/callback/{token}`)

1. Compare the token with `hmac.compare_digest`. A wrong token gets 404 with an empty body and nothing else happens.
2. Read at most `CALLBACK_MAX_BYTES`; a larger body gets 413.
3. Parse the JSON. A malformed or unexpected body is logged as one `error` event (short reason, truncated body) and still acknowledged.
4. Store a `callback` event (its own committed transaction, `payment_id` null). Then look up the payment by `CheckoutRequestID` and attach its id to the event, the only update `payment_events` allows.
5. Apply the result (0 success, 1032 cancelled, 1037 timeout, anything else failed). `Amount` and `MpesaReceiptNumber` are matched by `Name`. `PhoneNumber` is masked by Safaricom and ignored completely.
6. Unknown checkout id: the event stays an orphan. `rematch_orphan_events` applies it once the payment has its checkout id (right after it becomes `initiated`, and during reconciliation).
7. Always return 200 `{"ResultCode": 0, "ResultDesc": "Accepted"}`, so Daraja never retries.

`apply_payment_result` is idempotent and safe to run concurrently. It finds the payment with a plain select, locks the order and then the payment, re-reads the payment, and returns `applied`, `duplicate`, `mismatch` or `unmatched`. It never raises for these outcomes. `callback_received_at` is set only for callbacks, and all timestamps use the database clock.

## Reconciliation (on `GET /orders/{id}` by the owning buyer)

1. Load the latest payment. If there is none, stop.
2. `initiating`, no checkout id, older than `MPESA_STALE_INITIATING_SECONDS`: lock the order, then the payment, and set it `failed` (`request_not_sent`).
3. `initiated` and older than `RECONCILE_AFTER_SECONDS`, or `timeout` and younger than `MPESA_TIMEOUT_RECONCILE_WINDOW_MINUTES`: first apply orphan callbacks. If the payment is still open and the last `status_query` event is older than `MPESA_QUERY_MIN_INTERVAL_SECONDS`, call STK Push Query and log a `status_query` event. `pending` changes nothing; `success`, `cancelled`, `timeout` and `failed` are applied with `source='query'`. A query result has no receipt and no amount, so the amount check is skipped and the receipt stays null.
4. Never raises: failures are logged as `error` events and the order is returned as usual. No transaction is open during the query. Admin reads never reconcile.

## Lock order

Everywhere: **orders, then payments, then listings**. This is the order used by `public.expire_stale_orders()`. Payment code locks an order and then its payment, and never locks a listing. Checkout locks only a listing and then inserts a new order. Cancel locks the order, then the listing.

## Events and secrets

`payment_events` is append-only. Each event is written in its own short transaction, so it survives a rollback of the caller. Payloads are capped at 16 KB. They never contain the Daraja password, passkey, consumer key or secret, access tokens, the callback token or unmasked phone numbers: `redact()` removes `Password`, `Authorization` and `access_token`, masks phone fields to the last 3 digits, and hides the callback token in `CallBackURL`. The uvicorn access log rewrites `/payments/callback/<token>` to `/payments/callback/***`.

## Daraja assumptions (re-verify against the Daraja documentation)

1. OAuth: `GET /oauth/v1/generate?grant_type=client_credentials` with HTTP basic auth returns `access_token` and `expires_in`. The token is cached until `expires_in` minus 60 seconds; a 401 refreshes it once and retries the call once.
2. `Timestamp` is `YYYYMMDDHHMMSS` in Africa/Nairobi time; `Password` is `base64(shortcode + passkey + timestamp)` with the same timestamp.
3. STK Push returns `MerchantRequestID`, `CheckoutRequestID`, `ResponseCode` (`"0"` accepted), `ResponseDescription`, `CustomerMessage`. `AccountReference` is at most 12 characters, `TransactionDesc` at most 13.
4. Callback: `Body.stkCallback` with `MerchantRequestID`, `CheckoutRequestID`, integer `ResultCode`, `ResultDesc` and, on success, `CallbackMetadata.Item` (`Amount`, `MpesaReceiptNumber`, `TransactionDate`, masked `PhoneNumber`).
5. Result codes: 0 success, 1032 cancelled, 1037 unreachable or timeout, 1 insufficient balance, 2001 wrong PIN.
6. STK Push Query returns `ResultCode` as a string, without receipt or amount. While the customer has not answered, it returns HTTP 500 with `errorCode` `500.001.1001`, which means pending.
7. Only one STK request at a time per phone; Daraja may reject a second.
8. Timeouts: connect 5 seconds, read 20 seconds. Token fetch and query retry one connection error; STK Push never retries.

## "Pay with M-Pesa" message sequence

Participants: Buyer, PWA, API, Database, Daraja (sandbox), Phone.

1. Buyer taps "Pay with M-Pesa" on a `pending_payment` order where `can_pay` is true.
2. PWA → API: `POST /orders/{id}/pay` with the bearer token and a new `Idempotency-Key`, empty body.
3. API → Database: look up the stored idempotency key (a replay returns 202 with the current order at once, without steps 4 to 9).
4. API → Database: lock the order (`FOR UPDATE`), check status, time left (database clock), open payments, attempts and per-phone limit.
5. API → Database: insert payment `initiating` (amount = order total), commit (lock released).
6. API → Database: insert `stk_request` event.
7. API → Daraja: OAuth token (cached), then STK Push request with `CallBackURL`.
8. Daraja → API: `ResponseCode "0"`, `CheckoutRequestID`, `MerchantRequestID`.
9. API → Database: lock order, then payment; save ids; status `initiated`; commit; insert `stk_response` event; apply any orphan callback.
10. API → PWA: 202 with the order, "Check your phone and enter your M-Pesa PIN." and "Prompt sent to 254*******149".
11. Daraja → Phone: STK prompt. Buyer enters the PIN on the phone.
12. Daraja → API: `POST /payments/callback/{token}` with `Body.stkCallback`.
13. API → Database: insert `callback` event, attach the payment id, lock order then payment, set payment `completed` (receipt) and order `paid`, commit.
14. API → Daraja: 200 `{"ResultCode": 0, "ResultDesc": "Accepted"}`.
15. PWA → API: poll `GET /orders/{id}` every few seconds.
16. API → Database: (if no callback arrived after `RECONCILE_AFTER_SECONDS`) API → Daraja STK Push Query → apply the result.
17. API → PWA: order `paid` with payment `completed` and its receipt; the PWA shows "Payment received" and the sandbox note.
