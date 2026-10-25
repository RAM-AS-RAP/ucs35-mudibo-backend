"""IP and per email rate-limit tests without database access."""

from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.errors import InvalidCredentials, TooManyAttempts
from app.db import get_db
from app.routers import auth as auth_router
from app.services import auth_service, registration_service
from app.services.auth_service import LoginThrottle


async def fake_db():
    yield object()


@pytest.mark.asyncio
async def test_sixth_login_attempt_from_ip_is_rate_limited(api_client, application, monkeypatch) -> None:
    application.dependency_overrides[get_db] = fake_db

    async def invalid_login(session, email, password):
        raise InvalidCredentials()

    monkeypatch.setattr(auth_service, "login", invalid_login)
    auth_router.limiter._storage.reset()
    responses = []
    for attempt in range(6):
        response = await api_client.post(
            "/api/v1/auth/login", json={"email": "buyer@example.com", "password": f"wrong-{attempt}"}
        )
        responses.append(response)
    assert all(response.status_code == 401 for response in responses[:5])
    assert responses[5].status_code == 429
    assert responses[5].json()["error"]["code"] == "rate_limited"
    auth_router.limiter._storage.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "limit"),
    [
        ("/api/v1/auth/login/verify", {"challenge_token": "t" * 43, "code": "ABCD-EFGH"}, 10),
        ("/api/v1/auth/register/verify", {"registration_token": "t" * 43, "code": "ABCD-EFGH"}, 5),
    ],
)
async def test_code_verification_is_rate_limited_per_ip(api_client, application, monkeypatch, path, body, limit):
    application.dependency_overrides[get_db] = fake_db

    async def wrong_code(*args, **kwargs):
        raise auth_service.login_code_invalid()

    monkeypatch.setattr(auth_service, "verify_login", wrong_code)
    monkeypatch.setattr(registration_service, "verify", wrong_code)
    statuses = [(await api_client.post(path, json=body)).status_code for _ in range(limit + 1)]
    assert statuses == [400] * limit + [429]


@pytest.mark.asyncio
async def test_email_throttle_blocks_correct_password_until_window_expires(monkeypatch) -> None:
    now = [100.0]
    throttle = LoginThrottle(max_failures=5, window_minutes=1, clock=lambda: now[0])
    monkeypatch.setattr(auth_service, "get_login_throttle", lambda: throttle)
    monkeypatch.setattr(get_settings(), "login_otp_roles", "admin")
    monkeypatch.setattr(
        auth_service,
        "authenticate",
        _invalid_authentication,
    )
    for _ in range(5):
        with pytest.raises(InvalidCredentials):
            await auth_service.login(SimpleNamespace(), "buyer@example.com", "wrong")

    async def successful_authentication(session, email, password):
        return SimpleNamespace(
            id="user-1", role=SimpleNamespace(value="buyer"), is_super_admin=False, must_change_password=False
        )

    monkeypatch.setattr(auth_service, "authenticate", successful_authentication)
    with pytest.raises(TooManyAttempts):
        await auth_service.login(SimpleNamespace(), "BUYER@example.com", "correct-password")
    now[0] += 61
    result = await auth_service.login(SimpleNamespace(), "buyer@example.com", "correct-password")
    assert result.status == "signed_in" and result.role == "buyer"


async def _invalid_authentication(session, email, password):
    raise InvalidCredentials()
