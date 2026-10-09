"""Public health endpoints and security headers without a database connection."""

from contextlib import asynccontextmanager

import pytest

from app import main


@pytest.mark.asyncio
async def test_health_does_not_touch_database(api_client, monkeypatch) -> None:
    def unexpected_engine():
        raise AssertionError("basic health must not access the database")

    monkeypatch.setattr(main, "get_engine", unexpected_engine)
    response = await api_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_database_health_reports_ok_for_select_one(api_client, monkeypatch) -> None:
    class Connection:
        async def execute(self, statement):
            assert str(statement).lower() == "select 1"

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

    monkeypatch.setattr(main, "get_engine", lambda: Engine())
    response = await api_client.get("/api/v1/health/db")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_database_health_hides_connection_error(api_client, monkeypatch) -> None:
    def unavailable_engine():
        raise RuntimeError("private database detail")

    monkeypatch.setattr(main, "get_engine", unavailable_engine)
    response = await api_client.get("/api/v1/health/db")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}
    assert "private database detail" not in response.text
