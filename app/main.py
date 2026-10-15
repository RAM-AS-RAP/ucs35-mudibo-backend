"""FastAPI application factory and versioned router registration."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.errors import error_body, install_error_handlers
from app.db import get_engine
from app.integrations import email as email_integration
from app.integrations.daraja import DarajaClient, DarajaConfigError, redact_callback_url
from app.routers import admin, auth, forecasts, listings, markets, orders, payments, prices

logger = logging.getLogger(__name__)


class CallbackTokenFilter(logging.Filter):
    """Rewrite ``/payments/callback/<token>`` to ``/payments/callback/***`` in access log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_callback_url(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        return True


def _redact_arg(arg: object) -> object:
    """Redact strings and URL objects (httpx logs ``httpx.URL``) that contain a callback path."""
    if isinstance(arg, str):
        return redact_callback_url(arg)
    if not isinstance(arg, int | float | bytes) and "/payments/callback/" in str(arg):
        return redact_callback_url(str(arg))
    return arg


def install_access_log_filter() -> None:
    """Install the callback token filter once on uvicorn's access logger and on httpx's request logger."""
    for name in ("uvicorn.access", "httpx"):
        target = logging.getLogger(name)
        if not any(isinstance(existing, CallbackTokenFilter) for existing in target.filters):
            target.addFilter(CallbackTokenFilter())


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create one sandbox Daraja client for the application and close it on shutdown."""
    try:
        client: DarajaClient | None = DarajaClient(get_settings())
    except DarajaConfigError as exc:
        # Payment routes answer 503 payments_unavailable; the rest of the API still starts.
        logger.warning("Daraja client disabled: %s", exc)
        client = None
    application.state.daraja_client = client
    try:
        yield
    finally:
        if client is not None:
            await client.aclose()


def create_app() -> FastAPI:
    """Construct the API with environment-appropriate docs and middleware."""
    settings = get_settings()
    docs_enabled = settings.app_env != "production"
    install_access_log_filter()
    email_integration.log_startup_warnings(settings)
    application = FastAPI(
        title="AgriSence Backend API",
        version="1.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    application.state.limiter = auth.limiter
    application.add_middleware(SlowAPIMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @application.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        del request
        del exc
        return JSONResponse(status_code=429, content=error_body("rate_limited", "Too many requests"))

    install_error_handlers(application)

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.get("/api/v1/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/v1/health/db", tags=["health"])
    async def database_health() -> Any:
        try:
            async with get_engine().connect() as connection:
                await connection.execute(text("select 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"status": "degraded"})
        return {"status": "ok"}

    for route in (
        auth.router,
        admin.router,
        markets.router,
        prices.router,
        forecasts.router,
        listings.router,
        orders.router,
        payments.router,
    ):
        application.include_router(route, prefix="/api/v1")
    return application


app = create_app()
