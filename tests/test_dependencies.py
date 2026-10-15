"""Bearer, database role, disabled-account, and password-change guard tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.dependencies import get_current_user_any
from app.core.security import create_access_token, hash_password
from app.db import get_db
from app.models.enums import UserRole


class FakeSession:
    def __init__(self, user):
        self.user = user

    async def scalar(self, statement):
        del statement
        return self.user

    async def commit(self):
        return None


async def override_db(session):
    yield session


def make_db_override(session):
    async def dependency():
        yield session

    return dependency


def make_user(role=UserRole.FARMER, **overrides):
    values = {
        "id": uuid4(),
        "name": "Test User",
        "phone_number": "254712345678",
        "email": "test@example.com",
        "role": role,
        "is_disabled": False,
        "is_super_admin": False,
        "must_change_password": False,
        "password_hash": hash_password("temporary-password"),
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_missing_bearer_token_returns_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_any(None, FakeSession(None))
    assert exc_info.value.status_code == 401
    assert exc_info.value.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [None, make_user(is_disabled=True)])
async def test_deleted_or_disabled_user_returns_401(user) -> None:
    token = create_access_token(str(uuid4()), "farmer")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_any(credentials, FakeSession(user))
    assert exc_info.value.status_code == 401
    assert exc_info.value.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_database_role_overrides_admin_token_claim(api_client, application) -> None:
    farmer = make_user(role=UserRole.FARMER)
    application.dependency_overrides[get_db] = make_db_override(FakeSession(farmer))
    token = create_access_token(farmer.id, "admin")
    response = await api_client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_forced_password_user_can_only_use_auth_exceptions(api_client, application) -> None:
    user = make_user(must_change_password=True)
    application.dependency_overrides[get_db] = make_db_override(FakeSession(user))
    token = create_access_token(user.id, "farmer")
    headers = {"Authorization": f"Bearer {token}"}

    protected = await api_client.get("/api/v1/markets", headers=headers)
    assert protected.status_code == 403
    assert protected.json()["error"]["code"] == "password_change_required"

    current = await api_client.get("/api/v1/auth/me", headers=headers)
    assert current.status_code == 200
    assert "password_hash" not in current.json()
    assert "is_disabled" not in current.json()

    changed = await api_client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "temporary-password", "new_password": "New-password-123!"},
    )
    assert changed.status_code == 200
    assert changed.json()["login_required"] is True
    assert user.must_change_password is False


@pytest.mark.asyncio
async def test_super_admin_route_rejects_ordinary_admin(api_client, application) -> None:
    user = make_user(role=UserRole.ADMIN, is_super_admin=False)
    application.dependency_overrides[get_db] = make_db_override(FakeSession(user))
    token = create_access_token(user.id, "admin")
    response = await api_client.post(
        "/api/v1/admin/invites",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "New Admin", "phone_number": "0712345678", "email": "new@example.com"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
