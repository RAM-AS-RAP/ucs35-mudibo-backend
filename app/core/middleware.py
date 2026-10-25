"""Pure ASGI middleware: request ids, security headers, access log, unhandled errors and body limits.

These are plain ASGI classes rather than ``BaseHTTPMiddleware`` so that they never buffer bodies and so
that a response produced by any inner layer (CORS, host check, error handlers) passes through them.
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from app.core.errors import PayloadTooLarge, error_body, internal_error_response
from app.core.request_context import REQUEST_ID_HEADER, choose_request_id, request_id_var
from app.integrations.daraja import redact_callback_url

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

access_logger = logging.getLogger("app.access")
error_logger = logging.getLogger("app.errors")

CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"
# The interactive docs load scripts and styles, so they do not get the strict CSP (disabled in production).
DOCS_PATHS = ("/docs", "/redoc")

CALLBACK_PREFIX = "/api/v1/payments/callback/"
_LISTING_ITEM = re.compile(r"/api/v1/listings/[^/]+")
MULTIPART_OVERHEAD_BYTES = 65_536


async def catch_unhandled(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """Run ``app``; turn an unhandled exception into the generic 500 and log its traceback.

    The traceback goes to the log (with the request id from the context variable); the client gets only
    the generic message and the request id. If the response had already started, it is left truncated.
    """
    started = False

    async def tracking_send(message: Message) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            started = True
        await send(message)

    try:
        await app(scope, receive, tracking_send)
    except Exception as exc:  # noqa: BLE001 - nothing escapes to the server
        error_logger.error(
            "unhandled error on %s %s", scope.get("method"), redact_callback_url(scope.get("path", "")), exc_info=exc
        )
        if not started:
            await internal_error_response()(scope, receive, tracking_send)


class UnhandledErrorMiddleware:
    """Sits just inside CORS so a generic 500 still carries the CORS headers a browser needs to read it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await catch_unhandled(self.app, scope, receive, send)


class RequestContextMiddleware:
    """Outermost application middleware.

    Assigns the request id (a client ``X-Request-ID`` is reused only when it is a valid UUID), adds the
    cache and security headers to every response and writes one access log line per request. It also
    catches unhandled errors as a backstop for anything raised outside ``UnhandledErrorMiddleware``.
    """

    def __init__(self, app: ASGIApp, *, production: bool) -> None:
        self.app = app
        self.production = production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = choose_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token = request_id_var.set(request_id)
        path = scope.get("path", "")
        started = time.perf_counter()
        state = {"status": 500, "started": False}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["started"] = True
                self._apply_headers(MutableHeaders(scope=message), path, request_id)
            await send(message)

        try:
            await catch_unhandled(self.app, scope, receive, send_with_headers)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            safe_path = redact_callback_url(path)
            access_logger.info(
                "%s %s %s %.1fms",
                scope.get("method"),
                safe_path,
                state["status"],
                duration_ms,
                extra={
                    "method": scope.get("method"),
                    "path": safe_path,
                    "status": state["status"],
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)

    def _apply_headers(self, headers: MutableHeaders, path: str, request_id: str) -> None:
        headers[REQUEST_ID_HEADER] = request_id
        headers["Cache-Control"] = "no-store"
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        if not path.startswith(DOCS_PATHS):
            headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        if self.production:
            headers["Strict-Transport-Security"] = "max-age=31536000"


class BodySizeLimitMiddleware:
    """Reject bodies above the limit for the route, from ``Content-Length`` and while streaming.

    JSON and form routes get ``MAX_JSON_BODY_BYTES``; the two multipart listing routes get
    ``MAX_IMAGE_BYTES`` plus 64 KiB for the form fields and multipart framing. The Daraja callback is
    skipped here because it applies its own ``CALLBACK_MAX_BYTES`` rule.
    """

    def __init__(self, app: ASGIApp, *, max_json_bytes: int, max_image_bytes: int) -> None:
        self.app = app
        self.max_json_bytes = max_json_bytes
        self.max_multipart_bytes = max_image_bytes + MULTIPART_OVERHEAD_BYTES

    def limit_for(self, method: str, path: str) -> int | None:
        if path.startswith(CALLBACK_PREFIX):
            return None
        route = path.rstrip("/")
        if (method == "POST" and route == "/api/v1/listings") or (method == "PATCH" and _LISTING_ITEM.fullmatch(route)):
            return self.max_multipart_bytes
        return self.max_json_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("method", ""), scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise PayloadTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except PayloadTooLarge:
            # Raised outside a route (for example while middleware read the body); answer here.
            if response_started:
                raise
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413, content=error_body("payload_too_large", "The request body is too large.")
        )
        await response(scope, receive, send)
