"""Uniform HTTP and application error responses."""

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


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
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.reason = reason


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    """Build the public error response envelope."""
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    """Install handlers that keep all API errors in the same response shape."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        del request
        detail: Any = exc.detail
        code = "http_error"
        message = str(detail)
        if isinstance(detail, Mapping):
            code = str(detail.get("code", code))
            message = str(detail.get("message", message))
        return JSONResponse(status_code=exc.status_code, content=error_body(code, message), headers=exc.headers)

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
            details.append({"field": field, "message": str(error.get("msg", "Invalid value"))})
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "fields": sorted(fields),
                    "details": details,
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
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(NotImplementedError)
    async def not_implemented_handler(request: Request, exc: NotImplementedError) -> JSONResponse:
        del request
        del exc
        return JSONResponse(
            status_code=501, content=error_body("not_implemented", "This endpoint is not implemented yet")
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        del exc
        return JSONResponse(
            status_code=500, content=error_body("internal_error", "An unexpected server error occurred")
        )
