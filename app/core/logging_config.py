"""Logging setup: one configuration at startup, request ids on every record, and central redaction.

``configure_logging`` runs once (from ``create_app``). It adds a stream handler to the root logger when
none exists (JSON lines when ``LOG_FORMAT=json``, plain text otherwise) and attaches the request context
and redaction filters to every handler of the root and uvicorn loggers. Filters on a handler see records
from every child logger, which logger-level filters do not. The uvicorn access log keeps its own
callback token filter (``app.main.CallbackTokenFilter``).
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from app.core.request_context import get_request_id
from app.integrations.daraja import redact_callback_url
from app.integrations.email import mask_email
from app.utils.phone import mask_phone

ACCESS_LOGGER = "app.access"
CONTEXT_FIELDS = ("request_id", "method", "path", "status", "duration_ms")
_TARGET_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"

_SENSITIVE_KEY = (
    r"[\w-]*password|[\w-]*passwd|[\w-]*token|authorization|api[-_]?key|[\w-]*secret|passkey|pepper"
    r"|(?:invite_|reset_|verification_|registration_|login_)?code|credential|nonce"
)
_KEY_VALUE = re.compile(
    r"(?i)(?<![\w-])(" + _SENSITIVE_KEY + r")(?![\w-])"
    r"(\"?'?\s*[:=]\s*\"?'?)"
    r"((?:Bearer|Basic)\s+[^\s\"',;&}\]]+|[^\s\"',;&}\]]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
# Kenyan mobile numbers in 2547/2541, +254, 07/01 or bare 9 digit form, not inside a longer digit run.
_PHONE = re.compile(r"(?<![\d])(?:\+?254|0)?[17]\d{8}(?![\d])")
# Email addresses; services already log masked addresses, this catches any that slip through.
_EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

_configured = False


def redact_text(value: str) -> str:
    """Mask secret looking key/value pairs, bearer values, callback tokens, emails and phone numbers."""
    value = _KEY_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}***", value)
    value = _BEARER.sub(r"\1 ***", value)
    value = redact_callback_url(value)
    value = _EMAIL.sub(lambda match: mask_email(match.group(0)), value)
    return _PHONE.sub(lambda match: mask_phone(match.group(0)), value)


class RequestContextFilter(logging.Filter):
    """Give every record the request context fields so formatters can always reference them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = get_request_id() or "-"
        for name in CONTEXT_FIELDS[1:]:
            if not hasattr(record, name):
                setattr(record, name, None)
        return True


class RedactionFilter(logging.Filter):
    """Redact the formatted message and any traceback before a handler formats the record.

    The message is rendered once and stored without arguments. Uvicorn access records keep their
    argument tuple (its formatter unpacks it), so each string argument is redacted instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_agrisence_redacted", False):
            return True
        if record.name.startswith("uvicorn.access") and isinstance(record.args, tuple):
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        else:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 - a malformed log call must not break logging
                message = str(record.msg)
            record.msg = redact_text(message)
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        record._agrisence_redacted = True
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line with a fixed field set."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        for name in CONTEXT_FIELDS[1:]:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_text(self.formatException(record.exc_info))
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def install_filters(handler: logging.Handler) -> None:
    """Attach the request context and redaction filters to ``handler`` once."""
    if not any(isinstance(existing, RequestContextFilter) for existing in handler.filters):
        handler.addFilter(RequestContextFilter())
    if not any(isinstance(existing, RedactionFilter) for existing in handler.filters):
        handler.addFilter(RedactionFilter())


def build_formatter(log_format: str) -> logging.Formatter:
    return JsonFormatter() if log_format == "json" else logging.Formatter(_TEXT_FORMAT)


def configure_logging(log_format: str) -> None:
    """Configure logging once per process; later calls only re-attach filters to new handlers."""
    global _configured
    root = logging.getLogger()
    if not _configured:
        if not root.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(build_formatter(log_format))
            root.addHandler(handler)
            if root.level == logging.WARNING:
                root.setLevel(logging.INFO)
        if log_format == "json":
            for name in _TARGET_LOGGERS[1:]:
                for handler in logging.getLogger(name).handlers:
                    handler.setFormatter(JsonFormatter())
        _configured = True
    for name in _TARGET_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            install_filters(handler)
