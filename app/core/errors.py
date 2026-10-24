"""Uniform HTTP and application error responses."""

import logging
from collections.abc import Mapping
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.request_context import get_request_id

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "An unexpected server error occurred"

# Codes used when an HTTPException carries a plain string detail (for example Starlette's own 404 and 405).
_STATUS_CODES = {
    400: "bad_request",
    401: "invalid_credentials",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}

# Validation messages for these fields are dropped if they ever contain the submitted value.
_SENSITIVE_FIELDS = {"password", "current_password", "new_password", "code", "token", "identifier", "phone_number"}

# SQLSTATE values that mean "try again shortly": lock timeout, deadlock, serialization failure.
BUSY_SQLSTATES = {"55P03", "40P01", "40001"}
# Connection exceptions (class 08), server shutting down or starting, too many connections.
UNAVAILABLE_SQLSTATES = {"57P01", "57P02", "57P03", "53300"}


class ServiceError(Exception):
    """Base class for expected service-layer API errors."""

    status_code = 400
    code = "request_error"
    message = "The request could not be completed"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"
    message = "An account with these details already exists"


class InvalidCredentials(ServiceError):
    status_code = 401
    code = "invalid_credentials"
    message = "Invalid identifier or password"


class TooManyAttempts(ServiceError):
    status_code = 429
    code = "rate_limited"
    message = "Too many login attempts; try again later"


class AppError(ServiceError):
    """An expected API error with a stable status/code and optional safe reason."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        reason: str | None = None,
        extra: Mapping[str, str] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.reason = reason
        self.extra = dict(extra or {})
        # Seconds for the Retry-After header of a 429; None uses the default for the code.
        self.retry_after = retry_after


class PayloadTooLarge(StarletteHTTPException):
    """Raised when a request body passes the configured limit (declared or while streaming)."""

    def __init__(self) -> None:
        super().__init__(
            status_code=413,
            detail={"code": "payload_too_large", "message": "The request body is too large."},
        )


def error_body(code: str, message: str) -> dict[str, dict[str, Any]]:
    """Build the public error response envelope, including the current request id."""
    return {"error": {"code": code, "message": message, "request_id": get_request_id()}}


def error_response(status_code: int, code: str, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message), headers=dict(headers or {}))


def internal_error_response() -> JSONResponse:
    """The generic 500: only a fixed message and the request id, never exception text."""
    return error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE)


def _sqlstate(exc: BaseException) -> str | None:
    """Find a SQLSTATE on a SQLAlchemy wrapper, the adapted DBAPI error, or the asyncpg cause."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value:
                return value
        current = getattr(current, "orig", None) or current.__cause__
    return None


def classify_database_error(exc: BaseException) -> str:
    """Return ``busy``, ``unavailable`` or ``other`` for a database layer exception."""
    if isinstance(exc, sa_exc.TimeoutError):  # connection pool exhausted
        return "unavailable"
    state = _sqlstate(exc)
    if state in BUSY_SQLSTATES:
        return "busy"
    if state is not None and (state.startswith("08") or state in UNAVAILABLE_SQLSTATES):
        return "unavailable"
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return "unavailable"
    if state is None and isinstance(
        exc,
        sa_exc.InterfaceError | sa_exc.OperationalError | asyncpg.exceptions.InterfaceError | ConnectionError | OSError,
    ):
        return "unavailable"
    if isinstance(exc, asyncpg.exceptions.PostgresConnectionError | asyncpg.exceptions.ConnectionDoesNotExistError):
        return "unavailable"
    return "other"


def database_error_response(exc: BaseException) -> JSONResponse:
    """Map a database failure to 503 (retryable) or the generic 500, never exposing driver text or SQL."""
    kind = classify_database_error(exc)
    if kind == "busy":
        logger.warning("database busy: %s sqlstate=%s", type(exc).__name__, _sqlstate(exc))
        return error_response(
            503, "busy", "The service is busy. Please try again.", headers={"Retry-After": "1"}
        )
    if kind == "unavailable":
        logger.warning("database unavailable: %s", type(exc).__name__)
        return error_response(
            503,
            "service_unavailable",
            "The service is temporarily unavailable. Please try again shortly.",
            headers={"Retry-After": "5"},
        )
    logger.error("database error: %s sqlstate=%s", type(exc).__name__, _sqlstate(exc), exc_info=exc)
    return internal_error_response()


def _safe_validation_message(error: Mapping[str, Any], field: str) -> str:
    message = str(error.get("msg", "Invalid value"))
    submitted = error.get("input")
    if field.split(".")[-1] in _SENSITIVE_FIELDS and isinstance(submitted, str) and submitted and submitted in message:
        return "Invalid value"
    return message


def install_error_handlers(app: FastAPI) -> None:
    """Install handlers that keep all API errors in the same response shape."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        del request
        detail: Any = exc.detail
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = str(detail)
        if isinstance(detail, Mapping):
            code = str(detail.get("code", code))
            message = str(detail.get("message", message))
        return error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        del request
        fields: set[str] = set()
        details: list[dict[str, str]] = []
        for error in exc.errors():
            field = (
                ".".join(
                    str(part)
                    for part in error.get("loc", ())
                    if part not in {"body", "query", "path", "header", "form"}
                )
                or "request"
            )
            fields.add(field)
            details.append({"field": field, "message": _safe_validation_message(error, field)})
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "fields": sorted(fields),
                    "details": details,
                    "request_id": get_request_id(),
                }
            },
        )

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        del request
        body = error_body(exc.code, exc.message)
        reason = getattr(exc, "reason", None)
        if reason is not None:
            body["error"]["reason"] = reason
        for key, value in getattr(exc, "extra", {}).items():
            body["error"].setdefault(key, value)
        headers = {}
        if exc.status_code == 429:
            headers["Retry-After"] = str(_service_retry_after(exc))
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    @app.exception_handler(NotImplementedError)
    async def not_implemented_handler(request: Request, exc: NotImplementedError) -> JSONResponse:
        del request
        del exc
        return error_response(501, "not_implemented", "This endpoint is not implemented yet")

    async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        return database_error_response(exc)

    for database_error in (
        sa_exc.DBAPIError,
        sa_exc.TimeoutError,
        asyncpg.exceptions.PostgresError,
        asyncpg.exceptions.InterfaceError,
        ConnectionError,
    ):
        app.add_exception_handler(database_error, database_error_handler)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Normally unreachable: RequestContextMiddleware catches unhandled errors first. Kept as a backstop.
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return internal_error_response()


def _service_retry_after(exc: ServiceError) -> int:
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, int) and retry_after >= 1:
        return retry_after
    settings = get_settings()
    if isinstance(exc, TooManyAttempts):
        return settings.login_failure_window_minutes * 60
    if getattr(exc, "code", "") == "resend_cooldown":
        return settings.invite_resend_cooldown_seconds
    return 60
