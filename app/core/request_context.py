"""Per-request context shared by middleware, error handlers and log records."""

from contextvars import ContextVar
from uuid import UUID, uuid4

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the current request id, or None outside a request."""
    return request_id_var.get()


def choose_request_id(incoming: str | None) -> str:
    """Use a client supplied id only when it is a valid UUID (normalised); otherwise generate a new one."""
    if incoming:
        try:
            return str(UUID(incoming.strip()))
        except ValueError:
            pass
    return str(uuid4())
