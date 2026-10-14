"""Daraja sandbox client tests using httpx.MockTransport; no network."""

import asyncio
import base64
import json
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.daraja import (
    NAIROBI,
    SANDBOX_BASE_URL,
    STK_PUSH_PATH,
    STK_QUERY_PATH,
    DarajaClient,
    DarajaConfigError,
    DarajaRequestError,
    DarajaTimeout,
    redact,
)

FIXED_CLOCK = datetime(2026, 10, 14, 12, 30, 45, tzinfo=NAIROBI)
TIMESTAMP = "20261014123045"


def client_settings(**overrides):
    values = {
        "mpesa_env": "sandbox",
        "mpesa_base_url": SANDBOX_BASE_URL,
        "mpesa_shortcode": "174379",
        "mpesa_passkey": "test-passkey",
        "mpesa_consumer_key": "test-consumer-key",
        "mpesa_consumer_secret": "test-consumer-secret",
        "mpesa_transaction_desc": "Crop order",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Daraja:
    """Scriptable fake Daraja: per path, a list of handlers consumed in order (the last one repeats)."""

    def __init__(self, routes=None, delay=0):
        self.routes = {key: list(value) for key, value in (routes or {}).items()}
        self.calls = Counter()
        self.requests = []
        self.delay = delay

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[path] += 1
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if path == "/oauth/v1/generate" and path not in self.routes:
            return httpx.Response(200, json={"access_token": f"token-{self.calls[path]}", "expires_in": "3599"})
        handlers = self.routes[path]
        handler = handlers.pop(0) if len(handlers) > 1 else handlers[0]
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(request)
        return handler

    def client(self, **overrides) -> DarajaClient:
        return DarajaClient(
            client_settings(**overrides), transport=httpx.MockTransport(self), clock=lambda: FIXED_CLOCK
        )


ACCEPTED = httpx.Response(
    200,
    json={
        "MerchantRequestID": "29115-34620561-1",
        "CheckoutRequestID": "ws_CO_191220191020363925",
        "ResponseCode": "0",
        "ResponseDescription": "Success. Request accepted for processing",
        "CustomerMessage": "Success. Request accepted for processing",
    },
)


def query_response(code):
    return httpx.Response(200, json={"ResponseCode": "0", "ResultCode": code, "ResultDesc": f"code {code}"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"mpesa_base_url": "https://api.safaricom.co.ke"},
        {"mpesa_shortcode": "600000"},
        {"mpesa_env": "production"},
    ],
)
def test_non_sandbox_configuration_raises_at_construction(overrides):
    with pytest.raises(DarajaConfigError):
        DarajaClient(client_settings(**overrides))


async def test_password_uses_the_same_timestamp_as_the_body():
    daraja = Daraja({STK_PUSH_PATH: [ACCEPTED]})
    client = daraja.client()
    await client.stk_push("254708374149", 114, "K7QX2MPA", "https://example.test/api/v1/payments/callback/x")
    body = json.loads(daraja.requests[-1].content)
    assert body["Timestamp"] == TIMESTAMP
    assert body["Password"] == base64.b64encode(f"174379test-passkey{TIMESTAMP}".encode()).decode()
    assert body["BusinessShortCode"] == body["PartyB"] == "174379"
    assert body["TransactionType"] == "CustomerPayBillOnline"
    assert body["TransactionDesc"] == "Crop order"
    await client.aclose()


async def test_token_is_cached_between_calls():
    daraja = Daraja({STK_QUERY_PATH: [query_response("0")]})
    client = daraja.client()
    await client.stk_query("ws_CO_1")
    await client.stk_query("ws_CO_2")
    assert daraja.calls["/oauth/v1/generate"] == 1
    token_request = daraja.requests[0]
    expected = base64.b64encode(b"test-consumer-key:test-consumer-secret").decode()
    assert token_request.headers["Authorization"] == f"Basic {expected}"
    assert daraja.requests[1].headers["Authorization"] == "Bearer token-1"


async def test_concurrent_callers_fetch_the_token_once():
    daraja = Daraja(delay=0.01)
    client = daraja.client()
    tokens = await asyncio.gather(client.get_token(), client.get_token(), client.get_token())
    assert tokens == ["token-1"] * 3
    assert daraja.calls["/oauth/v1/generate"] == 1


async def test_a_401_refreshes_the_token_once_and_retries_once():
    daraja = Daraja({STK_QUERY_PATH: [httpx.Response(401, json={}), query_response("0")]})
    client = daraja.client()
    result = await client.stk_query("ws_CO_1")
    assert result.state == "success"
    assert daraja.calls["/oauth/v1/generate"] == 2
    assert daraja.calls[STK_QUERY_PATH] == 2
    assert daraja.requests[-1].headers["Authorization"] == "Bearer token-2"


async def test_stk_push_sends_an_integer_amount_and_parses_acceptance():
    daraja = Daraja({STK_PUSH_PATH: [ACCEPTED]})
    result = await daraja.client().stk_push("254708374149", 114, "K7QX2MPA", "https://x.test/cb")
    body = json.loads(daraja.requests[-1].content)
    assert body["Amount"] == 114 and isinstance(body["Amount"], int)
    assert b'"Amount":114' in daraja.requests[-1].content.replace(b" ", b"")
    assert result.accepted is True
    assert result.checkout_request_id == "ws_CO_191220191020363925"
    assert result.merchant_request_id == "29115-34620561-1"
    assert "Password" not in result.request
    assert result.request["PhoneNumber"] == "254*******149"


async def test_stk_push_accepts_an_integer_response_code():
    response = httpx.Response(200, json={"ResponseCode": 0, "CheckoutRequestID": "ws_CO_1"})
    result = await Daraja({STK_PUSH_PATH: [response]}).client().stk_push("254708374149", 50, "REF", "https://x/cb")
    assert result.accepted is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"ResponseCode": "1", "ResponseDescription": "Rejected"}),
        httpx.Response(400, json={"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid Amount"}),
        httpx.Response(500, json={"errorCode": "500.001.1001", "errorMessage": "Unable to lock subscriber"}),
    ],
)
async def test_a_rejected_stk_push_is_not_accepted(response):
    result = await Daraja({STK_PUSH_PATH: [response]}).client().stk_push("254708374149", 50, "REF", "https://x/cb")
    assert result.accepted is False
    assert result.checkout_request_id is None


async def test_stk_push_is_never_retried_on_a_network_error():
    daraja = Daraja({STK_PUSH_PATH: [httpx.ConnectError("boom"), ACCEPTED]})
    with pytest.raises(DarajaRequestError):
        await daraja.client().stk_push("254708374149", 50, "REF", "https://x/cb")
    assert daraja.calls[STK_PUSH_PATH] == 1


async def test_stk_push_timeout_raises_daraja_timeout_without_retry():
    daraja = Daraja({STK_PUSH_PATH: [httpx.ReadTimeout("slow"), ACCEPTED]})
    with pytest.raises(DarajaTimeout):
        await daraja.client().stk_push("254708374149", 50, "REF", "https://x/cb")
    assert daraja.calls[STK_PUSH_PATH] == 1


@pytest.mark.parametrize("amount", [114.0, True, 0, "114"])
async def test_stk_push_refuses_a_non_integer_amount(amount):
    with pytest.raises(ValueError):
        await Daraja().client().stk_push("254708374149", amount, "REF", "https://x/cb")


async def test_stk_query_retries_one_connection_error():
    daraja = Daraja({STK_QUERY_PATH: [httpx.ConnectError("blip"), query_response("0")]})
    result = await daraja.client().stk_query("ws_CO_1")
    assert result.state == "success"
    assert daraja.calls[STK_QUERY_PATH] == 2


async def test_stk_query_processing_error_means_pending():
    pending = httpx.Response(
        500, json={"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}
    )
    result = await Daraja({STK_QUERY_PATH: [pending]}).client().stk_query("ws_CO_1")
    assert (result.state, result.result_code) == ("pending", None)


@pytest.mark.parametrize(
    ("code", "state", "normalised"),
    [
        ("0", "success", 0),
        ("1032", "cancelled", 1032),
        ("1037", "timeout", 1037),
        ("2001", "failed", 2001),
        ("1", "failed", 1),
    ],
)
async def test_stk_query_maps_string_result_codes(code, state, normalised):
    result = await Daraja({STK_QUERY_PATH: [query_response(code)]}).client().stk_query("ws_CO_1")
    assert (result.state, result.result_code) == (state, normalised)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"errorCode": "400.002.02", "errorMessage": "Bad Request"}),
        httpx.Response(500, json={"errorCode": "500.003.02", "errorMessage": "System busy"}),
        httpx.Response(200, json={"ResponseCode": "0"}),
    ],
)
async def test_stk_query_other_failures_raise(response):
    with pytest.raises(DarajaRequestError):
        await Daraja({STK_QUERY_PATH: [response]}).client().stk_query("ws_CO_1")


def test_redact_removes_secrets_and_masks_phones():
    payload = {
        "Password": "c2VjcmV0",
        "Authorization": "Bearer abc",
        "access_token": "abc",
        "PartyA": "254708374149",
        "PhoneNumber": 254708374149,
        "msisdn": "254711222333",
        "CallBackURL": "https://x.test/api/v1/payments/callback/secret-token-value",
        "nested": [{"Name": "PhoneNumber", "Value": 254722000149}, {"Name": "Amount", "Value": 5}],
    }
    redacted = redact(payload)
    assert "Password" not in redacted and "Authorization" not in redacted and "access_token" not in redacted
    assert redacted["PartyA"] == redacted["PhoneNumber"] == "254*******149"
    assert redacted["msisdn"] == "254*******333"
    assert redacted["CallBackURL"] == "https://x.test/api/v1/payments/callback/***"
    assert redacted["nested"] == [{"Name": "PhoneNumber", "Value": "254*******149"}, {"Name": "Amount", "Value": 5}]
    assert payload["Password"] == "c2VjcmV0", "redact must return a copy"


async def test_exception_messages_never_contain_credentials():
    daraja = Daraja({"/oauth/v1/generate": [httpx.Response(401, json={})]})
    with pytest.raises(Exception) as exc_info:
        await daraja.client().stk_query("ws_CO_1")
    message = str(exc_info.value)
    for secret in ("test-passkey", "test-consumer-key", "test-consumer-secret"):
        assert secret not in message
