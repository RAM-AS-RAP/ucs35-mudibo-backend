"""Module 7 infrastructure: request ids, error bodies, body limits, headers, CORS, hosts, rate limits, DB errors.

Every test builds its own application from an explicit ``Settings`` object (the developer's ``.env`` is
not read) and talks to it through ``httpx.AsyncClient`` without a network or a database.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import exc as sa_exc

from app.core.config import Settings
from app.core.errors import InvalidCredentials
from app.core.logging_config import install_filters
from app.db import get_db
from app.main import create_app
from app.routers import auth as auth_router
from app.routers import payments as payments_router
from app.services import auth_service, payment_service

JWT_SECRET = "test-only-jwt-secret-at-least-thirty-two-characters"
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}
PRODUCTION = {
    "app_env": "production",
    "email_provider": "resend",
    "email_api_key": "re_test_placeholder",
    "email_from_address": "noreply@agrisence.test",
    "frontend_base_url": "https://app.agrisence.test",
    "allowed_hosts": "api.agrisence.test",
    "cors_origins": "https://app.agrisence.test",
}
FRONTEND = "https://app.agrisence.test"
CALLBACK_TOKEN = "cbt_" + "x" * 28


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, jwt_secret=JWT_SECRET, **overrides)


@asynccontextmanager
async def client_for(application: FastAPI, base_url: str = "http://test") -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=application, client=(f"test-{uuid4()}", 12345))
    async with AsyncClient(transport=transport, base_url=base_url) as client:
        yield client


@pytest.fixture(autouse=True)
def restore_limiter() -> Iterator[None]:
    yield
    auth_router.limiter.enabled = True


def assert_uuid(value: str) -> None:
    assert str(UUID(value)) == value


# Request id and error bodies -----------------------------------------------------------------


async def test_every_response_carries_a_request_id() -> None:
    async with client_for(create_app(make_settings())) as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert_uuid(response.headers["x-request-id"])


async def test_a_valid_client_request_id_is_reused() -> None:
    supplied = str(uuid4())
    async with client_for(create_app(make_settings())) as client:
        response = await client.get("/api/v1/does-not-exist", headers={"X-Request-ID": supplied})
    assert response.headers["x-request-id"] == supplied
    assert response.json()["error"]["request_id"] == supplied


@pytest.mark.parametrize("supplied", ["not-a-uuid", "<script>", "1" * 200])
async def test_an_invalid_client_request_id_is_ignored(supplied) -> None:
    async with client_for(create_app(make_settings())) as client:
        response = await client.get("/api/v1/health", headers={"X-Request-ID": supplied})
    assert response.headers["x-request-id"] != supplied
    assert_uuid(response.headers["x-request-id"])


async def test_unknown_route_uses_the_uniform_body() -> None:
    async with client_for(create_app(make_settings())) as client:
        response = await client.get("/api/v1/nothing-here")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_wrong_method_uses_the_uniform_body() -> None:
    async with client_for(create_app(make_settings())) as client:
        response = await client.delete("/api/v1/health")
    assert response.status_code == 405
    error = response.json()["error"]
    assert error["code"] == "method_not_allowed"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_validation_errors_carry_the_request_id_and_never_echo_a_password() -> None:
    secret = "Unique-Submitted-Secret-123!"
    application = create_app(make_settings())
    # The route's session dependency resolves before body validation; never let it need DATABASE_URL.
    application.dependency_overrides[get_db] = _fake_db
    async with client_for(application) as client:
        response = await client.post(
            "/api/v1/auth/register/farmer",
            json={"name": "A", "phone_number": "bad", "password": secret, "role": "admin"},
        )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["request_id"] == response.headers["x-request-id"]
    assert secret not in response.text


async def test_unhandled_exception_returns_the_generic_500_and_logs_the_traceback(caplog) -> None:
    application = create_app(make_settings())

    @application.get("/api/v1/test-boom")
    async def boom() -> None:
        raise RuntimeError("private internal detail")

    caplog.set_level(logging.INFO)
    install_filters(caplog.handler)
    async with client_for(application) as client:
        response = await client.get("/api/v1/test-boom")
    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected server error occurred", "request_id": request_id}
    }
    assert "private internal detail" not in response.text and "Traceback" not in response.text
    assert response.headers["cache-control"] == "no-store"
    logged = [record for record in caplog.records if record.exc_info]
    assert logged and logged[0].request_id == request_id
    assert "private internal detail" in caplog.text


# Body limits ---------------------------------------------------------------------------------


async def test_an_oversized_json_body_is_413() -> None:
    application = create_app(make_settings(max_json_body_bytes=1024))
    async with client_for(application) as client:
        response = await client.post("/api/v1/auth/login", json={"identifier": "x" * 2000, "password": "y"})
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "payload_too_large" and error["request_id"]


async def test_a_streamed_body_without_content_length_is_stopped_at_the_limit() -> None:
    application = create_app(make_settings(max_json_body_bytes=1024))

    async def chunks():
        for _ in range(10):
            yield b"x" * 512

    async with client_for(application) as client:
        response = await client.post(
            "/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_multipart_listing_routes_get_the_image_limit() -> None:
    application = create_app(make_settings(max_json_body_bytes=1024, max_image_bytes=4096))
    application.dependency_overrides[get_db] = _fake_db
    async with client_for(application) as client:
        too_big = await client.post(
            "/api/v1/listings", files={"image": ("a.png", b"x" * (4096 + 65536 + 1), "image/png")}
        )
        within = await client.patch(
            f"/api/v1/listings/{uuid4()}", files={"image": ("a.png", b"x" * 8192, "image/png")}
        )
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"
    # Above the JSON limit but within the multipart limit: the request reaches authentication.
    assert within.status_code == 401


async def test_the_callback_route_keeps_its_own_limit(monkeypatch) -> None:
    settings = make_settings(max_json_body_bytes=1024, mpesa_callback_token=CALLBACK_TOKEN)
    monkeypatch.setattr(payments_router, "get_settings", lambda: settings)
    received = []

    async def record(session_factory, body):
        received.append(len(body))

    monkeypatch.setattr(payment_service, "process_callback", record)
    application = create_app(settings)
    url = f"/api/v1/payments/callback/{CALLBACK_TOKEN}"
    async with client_for(application) as client:
        within = await client.post(url, content=b"{" + b" " * 4000 + b"}")
        too_big = await client.post(url, content=b"x" * (settings.callback_max_bytes + 1))
    assert within.status_code == 200 and received == [4002]
    assert too_big.status_code == 413
    assert too_big.content == b""  # the callback's own rule, not the JSON middleware


# Headers -------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/missing"])
async def test_security_headers_on_normal_and_error_responses(path) -> None:
    async with client_for(create_app(make_settings())) as client:
        response = await client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value, name
    assert "strict-transport-security" not in response.headers


async def test_docs_pages_skip_the_strict_csp() -> None:
    async with client_for(create_app(make_settings())) as client:
        response = await client.get("/docs")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"


async def test_hsts_only_in_production() -> None:
    application = create_app(make_settings(**PRODUCTION))
    async with client_for(application, base_url="https://api.agrisence.test") as client:
        response = await client.get("/api/v1/health")
        docs = await client.get("/docs")
    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert docs.status_code == 404  # docs are disabled in production


# CORS ----------------------------------------------------------------------------------------


async def test_an_allowed_origin_gets_the_allow_and_expose_headers() -> None:
    application = create_app(make_settings(cors_origins=FRONTEND))
    async with client_for(application) as client:
        response = await client.get("/api/v1/health", headers={"Origin": FRONTEND})
    assert response.headers["access-control-allow-origin"] == FRONTEND
    exposed = {item.strip().lower() for item in response.headers["access-control-expose-headers"].split(",")}
    assert {"x-request-id", "retry-after"} <= exposed
    assert "access-control-allow-credentials" not in response.headers


async def test_a_disallowed_origin_gets_no_allow_origin() -> None:
    application = create_app(make_settings(cors_origins=FRONTEND))
    async with client_for(application) as client:
        response = await client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
        preflight = await client.options(
            "/api/v1/orders",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in preflight.headers


async def test_preflight_accepts_the_frontend_headers() -> None:
    application = create_app(make_settings(cors_origins=FRONTEND))
    async with client_for(application) as client:
        response = await client.options(
            "/api/v1/orders",
            headers={
                "Origin": FRONTEND,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND
    allowed = {item.strip().lower() for item in response.headers["access-control-allow-headers"].split(",")}
    assert {"authorization", "content-type", "idempotency-key"} <= allowed
    assert response.headers["access-control-max-age"] == "600"


@pytest.mark.parametrize(
    "overrides",
    [
        {**PRODUCTION, "cors_origins": "*"},
        {**PRODUCTION, "cors_origins": "https://app.agrisence.test,*"},
        {"cors_origins": "https://app.agrisence.test/"},
        {"cors_origins": "https://app.agrisence.test/path"},
        {"cors_origins": "app.agrisence.test"},
        {"cors_origins": "ftp://app.agrisence.test"},
    ],
)
def test_bad_cors_origins_fail_settings_validation(overrides) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


# Allowed hosts -------------------------------------------------------------------------------


async def test_another_host_header_is_400() -> None:
    application = create_app(make_settings(allowed_hosts="api.agrisence.test"))
    async with client_for(application, base_url="http://evil.example") as client:
        refused = await client.get("/api/v1/health")
    async with client_for(application, base_url="http://api.agrisence.test") as client:
        accepted = await client.get("/api/v1/health")
    assert refused.status_code == 400
    assert accepted.status_code == 200


@pytest.mark.parametrize("hosts", ["", "*", "api.agrisence.test,*"])
def test_production_requires_explicit_allowed_hosts(hosts) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{**PRODUCTION, "allowed_hosts": hosts})


# Rate limits ---------------------------------------------------------------------------------


async def _fake_db():
    yield object()


async def _login_six_times(application: FastAPI, monkeypatch) -> list:
    async def invalid_login(session, identifier, password):
        raise InvalidCredentials()

    monkeypatch.setattr(auth_service, "login", invalid_login)
    application.dependency_overrides[get_db] = _fake_db
    auth_router.limiter._storage.reset()
    async with client_for(application) as client:
        return [
            await client.post("/api/v1/auth/login", json={"identifier": "buyer@example.com", "password": "wrong"})
            for _ in range(6)
        ]


async def test_rate_limited_responses_have_the_uniform_body_and_retry_after(monkeypatch) -> None:
    responses = await _login_six_times(create_app(make_settings()), monkeypatch)
    assert [response.status_code for response in responses] == [401] * 5 + [429]
    limited = responses[-1]
    error = limited.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["request_id"] == limited.headers["x-request-id"]
    assert 1 <= int(limited.headers["retry-after"]) <= 60


async def test_rate_limits_can_be_disabled_outside_production(monkeypatch) -> None:
    responses = await _login_six_times(create_app(make_settings(rate_limits_disabled=True)), monkeypatch)
    assert [response.status_code for response in responses] == [401] * 6


def test_disabling_rate_limits_fails_in_production() -> None:
    with pytest.raises(ValidationError):
        make_settings(**PRODUCTION, rate_limits_disabled=True)


# Settings ranges -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("log_format", "xml"),
        ("max_json_body_bytes", 100),
        ("db_statement_timeout_ms", -1),
        ("db_statement_timeout_ms", 50),
        ("db_lock_timeout_ms", 600_001),
        ("allowed_hosts", "https://api.agrisence.test"),
    ],
)
def test_new_settings_are_validated(field, value) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_new_settings_defaults() -> None:
    settings = make_settings()
    assert settings.log_format == "text"
    assert settings.max_json_body_bytes == 65536
    assert (settings.db_statement_timeout_ms, settings.db_lock_timeout_ms) == (15000, 5000)
    assert settings.allowed_host_list == ["*"]
    assert settings.rate_limits_disabled is False
    assert make_settings(db_statement_timeout_ms=0, db_lock_timeout_ms=0).db_lock_timeout_ms == 0


# Database error mapping ----------------------------------------------------------------------


class DriverError(Exception):
    """Stands in for an asyncpg error adapted by SQLAlchemy (which exposes ``sqlstate``)."""

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _app_raising(error: Exception) -> FastAPI:
    application = create_app(make_settings())

    @application.get("/api/v1/test-db-error")
    async def failing() -> None:
        raise error

    return application


async def _call(application: FastAPI):
    async with client_for(application) as client:
        return await client.get("/api/v1/test-db-error")


@pytest.mark.parametrize(
    "error",
    [
        sa_exc.OperationalError("select 1", {}, ConnectionRefusedError("db.internal.host refused")),
        sa_exc.InterfaceError("select 1", {}, DriverError("connection is closed db.internal.host")),
        sa_exc.OperationalError("select 1", {}, DriverError("db.internal.host shutting down", "57P01")),
        sa_exc.TimeoutError("QueuePool limit of size 5 overflow 5 reached db.internal.host"),
        ConnectionRefusedError("db.internal.host:6543 refused"),
    ],
    ids=["refused", "closed", "admin-shutdown", "pool-timeout", "raw-connect"],
)
async def test_connection_failures_are_503_service_unavailable(error) -> None:
    response = await _call(_app_raising(error))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "db.internal.host" not in response.text and "select" not in response.text.lower()


@pytest.mark.parametrize("sqlstate", ["55P03", "40P01", "40001"])
async def test_lock_timeouts_deadlocks_and_serialization_failures_are_503_busy(sqlstate) -> None:
    error = sa_exc.OperationalError(
        "update public.orders set status = 'paid'", {}, DriverError("canceling statement", sqlstate)
    )
    response = await _call(_app_raising(error))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["code"] == "busy"
    assert "public.orders" not in response.text and "canceling" not in response.text


async def test_an_unknown_database_error_is_the_generic_500_without_driver_text() -> None:
    error = sa_exc.ProgrammingError(
        "select secret_column from public.users", {}, DriverError('column "secret_column" does not exist', "42703")
    )
    response = await _call(_app_raising(error))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret_column" not in response.text and "public.users" not in response.text
