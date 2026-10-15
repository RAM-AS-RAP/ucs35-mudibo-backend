"""FastAPI application factory and versioned router registration."""

import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.core.errors import error_body, install_error_handlers
from app.core.logging_config import configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware, UnhandledErrorMiddleware
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


def rate_limit_retry_after(request: Request, exc: RateLimitExceeded) -> int:
    """Seconds until the exceeded slowapi window resets (at least 1).

    slowapi 0.1.10 can only write its own headers when every limited route returns a ``Response`` or
    takes a ``response`` parameter (``headers_enabled=True`` raises otherwise), so ``headers_enabled``
    stays off and ``Retry-After`` is computed here from the same window statistics slowapi would use.
    """
    current = getattr(request.state, "view_rate_limit", None)
    try:
        if current is not None:
            reset_at = auth.limiter.limiter.get_window_stats(current[0], *current[1])[0]
            return max(1, math.ceil(reset_at - time.time()))
    except Exception:  # noqa: BLE001 - fall back to the window length
        pass
    return max(1, int(exc.limit.limit.get_expiry()))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the API with environment-appropriate docs and middleware."""
    settings = settings or get_settings()
    docs_enabled = not settings.is_production
    configure_logging(settings.log_format)
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
    # RATE_LIMITS_DISABLED is refused in production by settings validation; a development and test aid.
    auth.limiter.enabled = not settings.rate_limits_disabled
    application.state.limiter = auth.limiter

    # Starlette runs the last added middleware first. Outermost to innermost: request context and
    # headers, CORS, unhandled errors, host check, slowapi, body limits. CORS sits outside the host
    # check, the body limit and the generic 500 so a browser can read those error bodies too. The body
    # limit is innermost so its streaming 413 reaches FastAPI's body parser unwrapped (slowapi's
    # BaseHTTPMiddleware would otherwise wrap the exception and FastAPI would answer a generic 400).
    application.add_middleware(
        BodySizeLimitMiddleware,
        max_json_bytes=settings.max_json_body_bytes,
        max_image_bytes=settings.max_image_bytes,
    )
    application.add_middleware(SlowAPIMiddleware)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    application.add_middleware(UnhandledErrorMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    application.add_middleware(RequestContextMiddleware, production=settings.is_production)

    @application.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content=error_body("rate_limited", "Too many requests"),
            headers={"Retry-After": str(rate_limit_retry_after(request, exc))},
        )

    install_error_handlers(application)

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
