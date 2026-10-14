"""Asynchronous Safaricom Daraja SANDBOX client for STK Push and STK Push Query.

The client can only talk to the sandbox: the constructor rejects any environment, base URL or
shortcode other than the sandbox values, and there is no setting that selects production.

Daraja assumptions used here. Re-verify each one against the Daraja documentation, and change only
this module if one turns out to be wrong:

1. OAuth: ``GET /oauth/v1/generate?grant_type=client_credentials`` with HTTP basic auth (consumer key
   and secret) returns ``access_token`` and ``expires_in`` (seconds, possibly as a string).
2. ``Timestamp`` is ``YYYYMMDDHHMMSS`` in Africa/Nairobi time, and ``Password`` is
   ``base64(shortcode + passkey + timestamp)`` built from that same timestamp value.
3. STK Push: ``POST /mpesa/stkpush/v1/processrequest`` returns ``MerchantRequestID``,
   ``CheckoutRequestID``, ``ResponseCode`` (the string ``"0"`` means accepted), ``ResponseDescription``
   and ``CustomerMessage``. ``AccountReference`` is at most 12 characters, ``TransactionDesc`` at most 13.
4. The callback body is ``Body.stkCallback`` with ``MerchantRequestID``, ``CheckoutRequestID``,
   ``ResultCode`` (an integer), ``ResultDesc`` and, on success, ``CallbackMetadata.Item``: a list of
   ``{Name, Value}`` with ``Amount``, ``MpesaReceiptNumber``, ``TransactionDate`` and ``PhoneNumber``.
   Safaricom masks that phone number, so it is never used for any lookup.
5. Result codes: 0 success, 1032 cancelled by the user, 1037 phone unreachable or timeout,
   1 insufficient balance, 2001 wrong PIN.
6. STK Push Query: ``POST /mpesa/stkpushquery/v1/query`` returns ``ResultCode`` as a STRING with no
   receipt number and no amount. While the customer has not answered yet, Daraja replies with HTTP 500
   and ``{"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}``, which
   means pending, not an error.
7. Only one STK request can be processed at a time on the same phone, so Daraja may reject a second.
8. The sandbox shortcode is 174379.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import Request

from app.utils.phone import mask_phone

SANDBOX_BASE_URL = "https://sandbox.safaricom.co.ke"
SANDBOX_SHORTCODE = "174379"
TOKEN_PATH = "/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_PATH = "/mpesa/stkpush/v1/processrequest"
STK_QUERY_PATH = "/mpesa/stkpushquery/v1/query"
QUERY_PENDING_ERROR_CODE = "500.001.1001"
TOKEN_EXPIRY_MARGIN_SECONDS = 60
DEFAULT_TOKEN_LIFETIME_SECONDS = 3599
MAX_ACCOUNT_REFERENCE_LENGTH = 12
MAX_DESCRIPTION_LENGTH = 200

RESULT_SUCCESS = 0
RESULT_CANCELLED = 1032
RESULT_TIMEOUT = 1037

OUTCOME_SUCCESS = "success"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"
STATE_PENDING = "pending"

NAIROBI = ZoneInfo("Africa/Nairobi")

_REMOVED_KEYS = {"password", "authorization", "access_token"}
_PHONE_KEYS = {"partya", "phonenumber", "msisdn"}
_CALLBACK_PATH = re.compile(r"/payments/callback/[^/?#\s]+")


class DarajaError(Exception):
    """Base class for Daraja client errors; messages never contain credentials or request bodies."""


class DarajaConfigError(DarajaError):
    """The client was configured with a non-sandbox environment, URL or shortcode."""


class DarajaAuthError(DarajaError):
    """Daraja rejected the OAuth credentials or the access token."""


class DarajaTimeout(DarajaError):
    """Daraja did not answer in time; the request may or may not have been processed."""


class DarajaRequestError(DarajaError):
    """Daraja could not be reached or returned an unusable response."""


def redact_callback_url(value: str) -> str:
    """Hide the secret callback token in a callback URL or request path."""
    return _CALLBACK_PATH.sub("/payments/callback/***", value)


def redact(payload: Any) -> Any:
    """Return a copy without passwords, authorization headers or tokens, with phone numbers masked.

    ``Password``, ``Authorization`` and ``access_token`` are removed. ``PartyA``, ``PhoneNumber`` and
    ``msisdn`` keep only their last 3 digits, and so does the ``Value`` of a callback metadata item named
    ``PhoneNumber``. A callback URL loses its secret token.
    """
    if isinstance(payload, dict):
        is_phone_item = str(payload.get("Name", "")).lower() in _PHONE_KEYS
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            name = str(key).lower()
            if name in _REMOVED_KEYS:
                continue
            if name in _PHONE_KEYS or (is_phone_item and name == "value"):
                redacted[key] = mask_phone(value)
            elif name == "callbackurl" and isinstance(value, str):
                redacted[key] = redact_callback_url(value)
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def outcome_for_result_code(result_code: int) -> str:
    """Map a Daraja result code to a payment outcome."""
    if result_code == RESULT_SUCCESS:
        return OUTCOME_SUCCESS
    if result_code == RESULT_CANCELLED:
        return OUTCOME_CANCELLED
    if result_code == RESULT_TIMEOUT:
        return OUTCOME_TIMEOUT
    return OUTCOME_FAILED


def parse_result_code(value: object) -> int | None:
    """Normalise a result code given as an integer or a numeric string; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    return None


def _short(value: object) -> str:
    return str(value or "")[:MAX_DESCRIPTION_LENGTH]


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@dataclass(frozen=True)
class StkPushResult:
    accepted: bool
    merchant_request_id: str | None
    checkout_request_id: str | None
    description: str
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StkQueryResult:
    state: str
    result_code: int | None
    description: str
    response: dict[str, Any] = field(default_factory=dict)


class DarajaClient:
    """Sandbox-only OAuth, STK Push and STK Push Query client sharing one ``httpx.AsyncClient``."""

    def __init__(
        self,
        settings: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if settings.mpesa_env != "sandbox":
            raise DarajaConfigError("MPESA_ENV must be sandbox")
        if settings.mpesa_base_url != SANDBOX_BASE_URL:
            raise DarajaConfigError("MPESA_BASE_URL must be the Daraja sandbox URL")
        if settings.mpesa_shortcode != SANDBOX_SHORTCODE:
            raise DarajaConfigError("MPESA_SHORTCODE must be the sandbox shortcode")
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(NAIROBI))
        self._http = httpx.AsyncClient(
            base_url=SANDBOX_BASE_URL,
            timeout=httpx.Timeout(20, connect=5),
            transport=transport,
        )
        self._token: str | None = None
        self._token_valid_until = 0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    def timestamp(self) -> str:
        return self._clock().strftime("%Y%m%d%H%M%S")

    def password(self, timestamp: str) -> str:
        raw = f"{self._settings.mpesa_shortcode}{self._settings.mpesa_passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode("ascii")

    async def _send(self, method: str, path: str, *, retry_network: bool, **kwargs: Any) -> httpx.Response:
        """Send one request; only token fetches and queries may retry a single connection failure."""
        attempts = 2 if retry_network else 1
        error: DarajaError = DarajaRequestError("Could not reach Daraja")
        for _ in range(attempts):
            try:
                return await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException:
                error = DarajaTimeout("Daraja did not respond in time")
            except httpx.TransportError:
                error = DarajaRequestError("Could not reach Daraja")
        raise error

    async def get_token(self) -> str:
        """Return a cached access token, fetching a new one at most once for concurrent callers."""
        async with self._token_lock:
            if self._token is not None and time.monotonic() < self._token_valid_until:
                return self._token
            return await self._fetch_token()

    async def _refresh_token(self, rejected: str) -> str:
        """Replace a token Daraja rejected with 401, unless another caller already replaced it."""
        async with self._token_lock:
            if self._token is not None and self._token != rejected and time.monotonic() < self._token_valid_until:
                return self._token
            return await self._fetch_token()

    async def _fetch_token(self) -> str:
        response = await self._send(
            "GET",
            TOKEN_PATH,
            retry_network=True,
            auth=(self._settings.mpesa_consumer_key, self._settings.mpesa_consumer_secret),
        )
        if response.status_code in (400, 401, 403):
            raise DarajaAuthError(f"Daraja rejected the OAuth credentials (HTTP {response.status_code})")
        if not response.is_success:
            raise DarajaRequestError(f"Daraja OAuth request failed (HTTP {response.status_code})")
        body = _json_body(response)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise DarajaAuthError("Daraja OAuth response did not contain an access token")
        lifetime = parse_result_code(body.get("expires_in"))
        if lifetime is None or lifetime <= 0:
            lifetime = DEFAULT_TOKEN_LIFETIME_SECONDS
        self._token = token
        self._token_valid_until = time.monotonic() + max(lifetime - TOKEN_EXPIRY_MARGIN_SECONDS, 0)
        return token

    async def _authorized_post(self, path: str, body: dict[str, Any], *, retry_network: bool) -> httpx.Response:
        """POST with a bearer token; a 401 refreshes the token once and retries the call once."""
        token = await self.get_token()
        response = await self._send(
            "POST", path, retry_network=retry_network, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == 401:
            token = await self._refresh_token(token)
            response = await self._send(
                "POST", path, retry_network=retry_network, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code == 401:
                raise DarajaAuthError("Daraja rejected the access token")
        return response

    async def stk_push(self, phone: str, amount: int, account_reference: str, callback_url: str) -> StkPushResult:
        """Send one STK Push request. It is NEVER retried automatically (only after a 401)."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("Amount must be a positive whole number of shillings")
        if not account_reference or len(account_reference) > MAX_ACCOUNT_REFERENCE_LENGTH:
            raise ValueError("AccountReference must be 1 to 12 characters")
        timestamp = self.timestamp()
        body = {
            "BusinessShortCode": self._settings.mpesa_shortcode,
            "Password": self.password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone,
            "PartyB": self._settings.mpesa_shortcode,
            "PhoneNumber": phone,
            "CallBackURL": callback_url,
            "AccountReference": account_reference,
            "TransactionDesc": self._settings.mpesa_transaction_desc,
        }
        response = await self._authorized_post(STK_PUSH_PATH, body, retry_network=False)
        data = _json_body(response)
        code = data.get("ResponseCode")
        checkout_request_id = data.get("CheckoutRequestID")
        accepted = (
            response.is_success
            and parse_result_code(code) == 0
            and isinstance(checkout_request_id, str)
            and bool(checkout_request_id)
        )
        merchant_request_id = data.get("MerchantRequestID")
        description = data.get("ResponseDescription") or data.get("errorMessage") or f"HTTP {response.status_code}"
        return StkPushResult(
            accepted=accepted,
            merchant_request_id=merchant_request_id if isinstance(merchant_request_id, str) else None,
            checkout_request_id=checkout_request_id if accepted else None,
            description=_short(description),
            request=redact(body),
            response=redact({"http_status": response.status_code, **data}),
        )

    async def stk_query(self, checkout_request_id: str) -> StkQueryResult:
        """Ask Daraja for the state of an STK request; one network retry is allowed."""
        timestamp = self.timestamp()
        body = {
            "BusinessShortCode": self._settings.mpesa_shortcode,
            "Password": self.password(timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        response = await self._authorized_post(STK_QUERY_PATH, body, retry_network=True)
        data = _json_body(response)
        logged = redact({"http_status": response.status_code, **data})
        if response.status_code == 500 and data.get("errorCode") == QUERY_PENDING_ERROR_CODE:
            return StkQueryResult(STATE_PENDING, None, _short(data.get("errorMessage")), logged)
        if not response.is_success:
            raise DarajaRequestError(f"Daraja STK query failed (HTTP {response.status_code})")
        result_code = parse_result_code(data.get("ResultCode"))
        if result_code is None:
            raise DarajaRequestError("Daraja STK query response had no result code")
        return StkQueryResult(outcome_for_result_code(result_code), result_code, _short(data.get("ResultDesc")), logged)


def get_daraja_client(request: Request) -> DarajaClient | None:
    """FastAPI dependency returning the application's shared client, or None when it is not available."""
    return getattr(request.app.state, "daraja_client", None)
