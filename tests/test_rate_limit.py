"""IP and identifier rate-limit tests without database access."""

from types import SimpleNamespace

import pytest

from app.core.errors import InvalidCredentials, TooManyAttempts
from app.db import get_db
from app.routers import auth as auth_router
from app.services import auth_service
from app.services.auth_service import LoginThrottle


async def fake_db():
    yield object()


@pytest.mark.asyncio
async def test_sixth_login_attempt_from_ip_is_rate_limited(api_client, application, monkeypatch) -> None:
    application.dependency_overrides[get_db] = fake_db

    async def invalid_login(session, identifier, password):
        raise InvalidCredentials()

    monkeypatch.setattr(auth_service, "login", invalid_login)
    auth_router.limiter._storage.reset()
    responses = []
    for attempt in range(6):
        response = await api_client.post(
            "/api/v1/auth/login", json={"identifier": "buyer@example.com", "password": f"wrong-{attempt}"}
        )
        responses.append(response)
    assert all(response.status_code == 401 for response in responses[:5])
    assert responses[5].status_code == 429
    assert responses[5].json()["error"]["code"] == "rate_limited"
    auth_router.limiter._storage.reset()


@pytest.mark.asyncio
async def test_identifier_throttle_blocks_correct_password_until_window_expires(monkeypatch) -> None:
    now = [100.0]
    throttle = LoginThrottle(max_failures=5, window_minutes=1, clock=lambda: now[0])
    monkeypatch.setattr(auth_service, "get_login_throttle", lambda: throttle)
    monkeypatch.setattr(
        auth_service,
        "authenticate",
        _invalid_authentication,
    )
    for _ in range(5):
        with pytest.raises(InvalidCredentials):
            await auth_service.login(SimpleNamespace(), "buyer@example.com", "wrong")

    async def successful_authentication(session, identifier, password):
        return SimpleNamespace(
            id="user-1", role=SimpleNamespace(value="buyer"), is_super_admin=False, must_change_password=False
        )

    monkeypatch.setattr(auth_service, "authenticate", successful_authentication)
    with pytest.raises(TooManyAttempts):
        await auth_service.login(SimpleNamespace(), "BUYER@example.com", "correct-password")
    now[0] += 61
    result = await auth_service.login(SimpleNamespace(), "buyer@example.com", "correct-password")
    assert result["role"] == "buyer"


async def _invalid_authentication(session, identifier, password):
    raise InvalidCredentials()
