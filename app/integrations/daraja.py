"""Asynchronous Safaricom Daraja sandbox client."""

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import Settings, get_settings


class DarajaError(RuntimeError):
    """Raised when Daraja rejects a request or returns an unusable response."""


def redact(value: Any) -> Any:
    """Recursively remove credentials and STK passwords before persistence/logging."""
    if isinstance(value, dict):
        secret_names = {
            "password",
            "accesstoken",
            "consumertoken",
            "consumerkey",
            "consumersecret",
            "passkey",
            "token",
            "authorization",
            "callbacktoken",
        }
        return {
            key: "[REDACTED]" if re.sub(r"[^a-z]", "", key.lower()) in secret_names else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class DarajaClient:
    """Minimal OAuth and STK client, restricted to the Daraja sandbox."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.mpesa_env != "sandbox":
            raise ValueError("Daraja production payments are not supported")
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=UTC)
        self._token_lock = asyncio.Lock()
        self.timeout = httpx.Timeout(20.0)

    async def _request_with_network_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Retry one network failure for safe OAuth/query requests only."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(2):
                try:
                    response = await client.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt:
                        raise
            raise DarajaError("Daraja network request failed")

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and datetime.now(UTC) < self._token_expires_at - timedelta(seconds=30):
                return self._token
            url = f"{self.settings.mpesa_base_url}/oauth/v1/generate?grant_type=client_credentials"
            try:
                response = await self._request_with_network_retry(
                    "GET",
                    url,
                    auth=(self.settings.mpesa_consumer_key, self.settings.mpesa_consumer_secret),
                )
            except httpx.HTTPError as exc:
                raise DarajaError("Daraja OAuth request failed") from exc
            body = response.json()
            token = body.get("access_token")
            if not isinstance(token, str):
                raise DarajaError("Daraja OAuth response did not contain an access token")
            self._token = token
            self._token_expires_at = datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 3600)))
            return token

    async def stk_push(self, phone: str, amount: int, account_reference: str, callback_url: str) -> dict[str, Any]:
        """Send one STK push; this operation is deliberately never retried."""
        if len(account_reference) > 12:
            raise ValueError("AccountReference must be at most 12 characters")
        token = await self._access_token()
        timestamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(
            f"{self.settings.mpesa_shortcode}{self.settings.mpesa_passkey}{timestamp}".encode()
        ).decode("ascii")
        request_body = {
            "BusinessShortCode": self.settings.mpesa_shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": phone,
            "PartyB": self.settings.mpesa_shortcode,
            "PhoneNumber": phone,
            "CallBackURL": callback_url,
            "AccountReference": account_reference,
            "TransactionDesc": "Mudibo order payment",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.settings.mpesa_base_url}/mpesa/stkpush/v1/processrequest",
                    json=request_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                raise DarajaError("Daraja STK request failed") from exc
        return response.json()

    async def stk_query(self, checkout_request_id: str) -> dict[str, Any]:
        """Query the state of an existing STK request; network errors retry once."""
        token = await self._access_token()
        timestamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(
            f"{self.settings.mpesa_shortcode}{self.settings.mpesa_passkey}{timestamp}".encode()
        ).decode("ascii")
        body = {
            "BusinessShortCode": self.settings.mpesa_shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        response = await self._request_with_network_retry(
            "POST",
            f"{self.settings.mpesa_base_url}/mpesa/stkpushquery/v1/query",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.json()
