"""``docs/openapi.json`` is the frontend contract and must match the application's schema."""

import pytest

from app.main import create_app
from scripts.export_openapi import OPENAPI_PATH, render_openapi

INSTRUCTION = "run `python scripts/export_openapi.py` and commit docs/openapi.json"


def test_openapi_file_matches_the_application() -> None:
    if not OPENAPI_PATH.exists():
        pytest.skip(f"docs/openapi.json does not exist yet: {INSTRUCTION}")
    current = render_openapi(create_app())
    stored = OPENAPI_PATH.read_text(encoding="utf-8")
    if current != stored:
        pytest.fail(f"docs/openapi.json is out of date: {INSTRUCTION}")


def test_export_is_deterministic() -> None:
    assert render_openapi(create_app()) == render_openapi(create_app())
