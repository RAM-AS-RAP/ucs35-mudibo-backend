"""Helpers for comparing API responses in tests."""

from typing import Any


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """Return an error body without its per-request ``request_id`` (which must be present)."""
    error = dict(body["error"])
    assert error.pop("request_id", None), "error bodies always carry a request_id"
    return {**body, "error": error}
