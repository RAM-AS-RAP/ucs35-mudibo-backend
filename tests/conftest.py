"""Shared async API test fixtures without a real database or network."""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

if len(os.environ.get("JWT_SECRET", "")) < 32:
    os.environ["JWT_SECRET"] = "test-only-jwt-secret-at-least-thirty-two-characters"

from app.core.config import get_settings
from app.db import get_engine
from app.main import create_app
from app.routers.auth import limiter


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip DB tests by default and refuse them under production configuration."""
    database_items = [item for item in items if item.get_closest_marker("db")]
    if database_items and get_settings().app_env.lower() == "production":
        raise pytest.UsageError("Database tests are forbidden when APP_ENV=production")
    if os.environ.get("RUN_DB_TESTS") != "1":
        skip = pytest.mark.skip(reason="set RUN_DB_TESTS=1 to enable database tests")
        for item in database_items:
            item.add_marker(skip)


@pytest.fixture
def application() -> FastAPI:
    return create_app()


@pytest.fixture(autouse=True)
def reset_login_rate_limiter() -> AsyncIterator[None]:
    limiter._storage.reset()
    yield
    limiter._storage.reset()


@pytest.fixture
async def api_client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    client_id = f"test-{uuid4()}"
    async with AsyncClient(
        transport=ASGITransport(app=application, client=(client_id, 12345)), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def db_session(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession]:
    """Provide a rollback-only savepoint session to explicitly marked DB tests."""
    if request.node.get_closest_marker("db") is None:
        pytest.fail("db_session is only available to tests marked with @pytest.mark.db")
    engine = get_engine()
    async with engine.connect() as connection:
        outer_transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await outer_transaction.rollback()
