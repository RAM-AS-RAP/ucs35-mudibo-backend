"""``is_super_admin`` in the login, profile, registration and accept-invite responses.

The flag always comes from the users row. It is never a token claim and never a request field.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from response_helpers import without_request_id

from app.core.security import decode_access_token, hash_password
from app.db import get_db
from app.models.enums import UserRole
from app.schemas.auth import TokenResponse
from app.schemas.user import UserOut
from app.services import auth_service, invite_service

PASSWORD = "Correct-horse-9!"
PASSWORD_HASH = hash_password(PASSWORD)


class FakeSession:
    """Answers every user lookup with the same row, as the login and bearer queries expect."""

    def __init__(self, user):
        self.user = user

    async def scalar(self, statement):
        del statement
        return self.user

    async def commit(self):
        return None


def install_session(application, session) -> None:
    async def dependency():
        yield session

    application.dependency_overrides[get_db] = dependency


def make_user(role: UserRole, *, super_admin: bool = False, **overrides):
    values = {
        "id": uuid4(),
        "name": f"Test {role.value}",
        "phone_number": "254712345678",
        "email": f"{role.value}@example.com",
        "password_hash": PASSWORD_HASH,
        "role": role,
        "is_super_admin": super_admin,
        "is_disabled": False,
        "must_change_password": False,
        "password_changed_at": None,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


ACCOUNTS = [
    pytest.param(UserRole.ADMIN, True, id="super_admin"),
    pytest.param(UserRole.ADMIN, False, id="admin"),
    pytest.param(UserRole.FARMER, False, id="farmer"),
    pytest.param(UserRole.BUYER, False, id="buyer"),
]


async def login(api_client, application, user):
    install_session(application, FakeSession(user))
    auth_service.get_login_throttle().reset()
    return await api_client.post("/api/v1/auth/login", json={"identifier": user.email, "password": PASSWORD})


def test_response_schemas_declare_the_flag() -> None:
    assert "is_super_admin" in UserOut.model_fields
    assert "is_super_admin" in TokenResponse.model_fields


@pytest.mark.parametrize(("role", "super_admin"), ACCOUNTS)
async def test_login_returns_the_flag_from_the_database_row(api_client, application, role, super_admin) -> None:
    response = await login(api_client, application, make_user(role, super_admin=super_admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_super_admin"] is super_admin
    assert body["role"] == role.value
    assert set(body) == {"access_token", "token_type", "expires_in", "role", "is_super_admin", "must_change_password"}


@pytest.mark.parametrize(("role", "super_admin"), ACCOUNTS)
async def test_token_payload_never_contains_the_flag(api_client, application, role, super_admin) -> None:
    response = await login(api_client, application, make_user(role, super_admin=super_admin))
    claims = decode_access_token(response.json()["access_token"])
    assert "is_super_admin" not in claims
    assert set(claims) == {"sub", "role", "iat", "exp"}


@pytest.mark.parametrize(("role", "super_admin"), ACCOUNTS)
async def test_me_returns_the_flag_from_the_database_row(api_client, application, role, super_admin) -> None:
    user = make_user(role, super_admin=super_admin)
    token = (await login(api_client, application, user)).json()["access_token"]
    response = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_super_admin"] is super_admin
    assert set(body) == {
        "id",
        "name",
        "phone_number",
        "email",
        "role",
        "is_super_admin",
        "must_change_password",
        "created_at",
    }
    assert "password_hash" not in body and "is_disabled" not in body


async def test_me_follows_the_row_not_the_login_response(api_client, application) -> None:
    user = make_user(UserRole.ADMIN, super_admin=False)
    token = (await login(api_client, application, user)).json()["access_token"]
    user.is_super_admin = True  # the row changes after the token was issued
    response = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["is_super_admin"] is True


async def test_disabled_user_still_gets_401(api_client, application) -> None:
    user = make_user(UserRole.ADMIN, super_admin=True)
    token = (await login(api_client, application, user)).json()["access_token"]
    user.is_disabled = True
    response = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    disabled_login = await login(api_client, application, user)
    assert disabled_login.status_code == 401
    assert without_request_id(disabled_login.json()) == {
        "error": {"code": "invalid_credentials", "message": "Invalid identifier or password"}
    }


@pytest.mark.parametrize(("path", "role"), [("farmer", UserRole.FARMER), ("buyer", UserRole.BUYER)])
async def test_registration_response_returns_false(api_client, application, monkeypatch, path, role) -> None:
    install_session(application, object())

    async def register(session, data, role):
        return make_user(role, name=data.name, phone_number=data.phone_number, email=None)

    monkeypatch.setattr(auth_service, "register_user", register)
    response = await api_client.post(
        f"/api/v1/auth/register/{path}",
        json={"name": "New Person", "phone_number": "0712345678", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    assert response.json()["is_super_admin"] is False
    assert response.json()["role"] == role.value


@pytest.mark.parametrize("path", ["farmer", "buyer"])
@pytest.mark.parametrize("value", [True, False])
async def test_registration_with_the_flag_in_the_body_is_422(api_client, application, monkeypatch, path, value) -> None:
    install_session(application, object())

    async def must_not_run(*args, **kwargs):
        raise AssertionError("registration must not run")

    monkeypatch.setattr(auth_service, "register_user", must_not_run)
    response = await api_client.post(
        f"/api/v1/auth/register/{path}",
        json={"name": "New Person", "phone_number": "0712345678", "password": PASSWORD, "is_super_admin": value},
    )
    assert response.status_code == 422
    assert "is_super_admin" in response.json()["error"]["fields"]


async def test_accept_invite_response_returns_false(api_client, application, monkeypatch) -> None:
    install_session(application, object())

    async def accepted(session, body):
        return {
            "id": uuid4(),
            "name": "Grace Admin",
            "phone_number": "254712000001",
            "email": "grace@example.com",
            "role": "admin",
            "is_super_admin": False,
            "must_change_password": False,
            "created_at": datetime.now(UTC),
        }

    monkeypatch.setattr(invite_service, "accept_invite", accepted)
    response = await api_client.post(
        "/api/v1/auth/accept-invite", json={"email": "grace@example.com", "code": "ABCD-EFGH", "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    assert response.json()["is_super_admin"] is False


def test_accept_invite_returns_the_flag_from_the_inserted_row() -> None:
    import inspect

    source = inspect.getsource(invite_service.accept_invite)
    assert "returning id, name, phone_number, email, cast(role as text) as role, is_super_admin," in source
    assert "'admin', false, false" in source
