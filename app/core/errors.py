"""Uniform HTTP and application error responses."""

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


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
        del exc
        return JSONResponse(status_code=422, content=error_body("validation_error", "Request validation failed"))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        del exc
        return JSONResponse(
            status_code=500, content=error_body("internal_error", "An unexpected server error occurred")
        )
