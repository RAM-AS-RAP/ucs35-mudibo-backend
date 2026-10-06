"""Shared async API test fixtures without a real database or network."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
def application() -> FastAPI:
    return create_app()


@pytest.fixture
async def api_client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        yield client
