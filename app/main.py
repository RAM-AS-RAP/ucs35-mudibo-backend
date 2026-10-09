"""FastAPI application factory and versioned router registration."""

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
from app.routers import admin, auth, forecasts, listings, markets, orders, payments, prices


def create_app() -> FastAPI:
    """Construct the API with environment-appropriate docs and middleware."""
    settings = get_settings()
    docs_enabled = settings.app_env != "production"
    application = FastAPI(
        title="AgriSence Backend API",
        version="1.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
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
