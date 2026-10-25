"""Access tokens issued before a password change are rejected; ``iat`` is compared by whole second."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.config import get_settings
from app.core.dependencies import get_current_user_any
from app.core.security import hash_password
from app.db import get_db
from app.models.enums import UserRole

CURRENT_PASSWORD = "Current-passw0rd!"


class FakeSession:
    def __init__(self, user):
        self.user = user
        self.commits = 0

    async def scalar(self, statement):
        del statement
        return self.user

    async def commit(self):
        self.commits += 1


def make_user(**overrides):
    values = {
        "id": uuid4(),
        "name": "Amina Farmer",
        "email": "amina@example.com",
        "email_verified_at": datetime.now(UTC),
        "role": UserRole.FARMER,
        "is_disabled": False,
        "is_super_admin": False,
        "must_change_password": False,
        "password_hash": hash_password(CURRENT_PASSWORD),
        "password_changed_at": None,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def token_issued_at(user_id, issued_at: int) -> str:
    settings = get_settings()
    claims = {"sub": str(user_id), "role": "farmer", "iat": issued_at, "exp": issued_at + 3600}
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def authenticate(user, token):
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return await get_current_user_any(credentials, FakeSession(user))


def now_seconds() -> int:
    return int(datetime.now(UTC).timestamp())


async def test_a_token_issued_before_a_password_change_is_401():
    issued = now_seconds() - 120
    user = make_user(password_changed_at=datetime.fromtimestamp(issued + 60, UTC))
    with pytest.raises(HTTPException) as exc_info:
        await authenticate(user, token_issued_at(user.id, issued))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == {"code": "invalid_credentials", "message": "Invalid authentication credentials"}


async def test_a_token_issued_after_a_password_change_works():
    issued = now_seconds()
    user = make_user(password_changed_at=datetime.fromtimestamp(issued - 30, UTC))
    assert await authenticate(user, token_issued_at(user.id, issued)) is user


async def test_a_token_issued_in_the_same_second_as_the_change_works():
    issued = now_seconds()
    user = make_user(password_changed_at=datetime.fromtimestamp(issued, UTC) + timedelta(milliseconds=700))
    assert await authenticate(user, token_issued_at(user.id, issued)) is user


async def test_a_user_who_never_changed_the_password_keeps_working():
    user = make_user()
    assert await authenticate(user, token_issued_at(user.id, now_seconds() - 600)) is user


async def test_change_password_revokes_the_token_used_for_it(api_client, application):
    user = make_user()
    session = FakeSession(user)

    async def request_session():
        yield session

    application.dependency_overrides[get_db] = request_session
    old_token = token_issued_at(user.id, now_seconds() - 5)
    headers = {"Authorization": f"Bearer {old_token}"}

    changed = await api_client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": CURRENT_PASSWORD, "new_password": "Brand-new-passw0rd!"},
    )

    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["login_required"] is True and "log in again" in body["message"].lower()
    # The service sets the database clock; the row now carries the change time.
    assert "now" in str(user.password_changed_at).lower()
    user.password_changed_at = datetime.now(UTC)
    again = await api_client.get("/api/v1/auth/me", headers=headers)
    assert again.status_code == 401
    fresh = token_issued_at(user.id, now_seconds())
    assert (await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {fresh}"})).status_code == 200


PERSONAL = "The password must not be your email or name."


@pytest.mark.parametrize(
    ("overrides", "new_password", "reason"),
    [
        ({"email": "Str0ng!Name@example.com"}, "Str0ng!Name@example.com", PERSONAL),
        ({"name": "Wanjiru#2026Farm"}, "wANJIRU#2026fARM", PERSONAL),
        ({}, "P@ssw0rd123", "This password is too common."),
    ],
)
async def test_change_password_applies_the_policy(api_client, application, overrides, new_password, reason):
    user = make_user(**overrides)
    session = FakeSession(user)

    async def request_session():
        yield session

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[get_current_user_any] = lambda: user
    response = await api_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": CURRENT_PASSWORD, "new_password": new_password},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "password_too_weak"
    assert response.json()["error"]["reason"] == reason
    assert session.commits == 0
    assert user.password_changed_at is None
