"""Route tests for fixed public registration roles and authorization."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from response_helpers import without_request_id

from app.core.dependencies import get_current_user
from app.core.errors import ConflictError
from app.core.security import hash_password
from app.db import get_db
from app.models.enums import UserRole
from app.services import auth_service


async def fake_db():
    yield object()


class FakeSession:
    def __init__(self, user):
        self.user = user

    async def scalar(self, statement):
        del statement
        return self.user

    async def commit(self):
        return None


async def session_override(session):
    yield session


def make_session_override(session):
    async def dependency():
        yield session

    return dependency


def make_user(role=UserRole.FARMER, **overrides):
    values = {
        "id": uuid4(),
        "name": "Farmer",
        "phone_number": "254712345678",
        "email": "farmer@example.com",
        "password_hash": hash_password("password123"),
        "role": role,
        "is_super_admin": False,
        "is_disabled": False,
        "must_change_password": False,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_registration_rejects_role_field(api_client, application) -> None:
    application.dependency_overrides[get_db] = fake_db
    response = await api_client.post(
        "/api/v1/auth/register/farmer",
        json={"name": "Farmer", "phone_number": "0712345678", "password": "password123", "role": "admin"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_farmer_registration_sets_role_in_route(api_client, application, monkeypatch) -> None:
    application.dependency_overrides[get_db] = fake_db
    called: dict[str, object] = {}

    async def register(session, data, role):
        called["role"] = role
        fields = data.model_dump()
        return SimpleNamespace(
            id=uuid4(),
            name=fields["name"],
            phone_number=fields["phone_number"],
            email=None,
            role=role,
            must_change_password=False,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(auth_service, "register_user", register)
    response = await api_client.post(
        "/api/v1/auth/register/farmer", json={"name": "Farmer", "phone_number": "0712345678", "password": "Goodpass1!"}
    )
    assert response.status_code == 201
    assert called["role"] == UserRole.FARMER
    assert response.json()["role"] == "farmer"


@pytest.mark.asyncio
async def test_buyer_registration_sets_role_in_route(api_client, application, monkeypatch) -> None:
    application.dependency_overrides[get_db] = fake_db

    async def register(session, data, role):
        return SimpleNamespace(
            id=uuid4(),
            name=data.name,
            phone_number=data.phone_number,
            email=None,
            role=role,
            must_change_password=False,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(auth_service, "register_user", register)
    response = await api_client.post(
        "/api/v1/auth/register/buyer",
        json={"name": "Buyer Name", "phone_number": "0112345678", "password": "Goodpass1!"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "buyer"


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_field", ["phone_number", "email"])
async def test_duplicate_registration_returns_generic_conflict(
    api_client, application, monkeypatch, duplicate_field
) -> None:
    del duplicate_field
    application.dependency_overrides[get_db] = fake_db

    async def conflict(session, data, role):
        raise ConflictError()

    monkeypatch.setattr(auth_service, "register_user", conflict)
    response = await api_client.post(
        "/api/v1/auth/register/farmer",
        json={
            "name": "Farmer Name",
            "phone_number": "0712345678",
            "email": "farmer@example.com",
            "password": "Goodpass1!",
        },
    )
    assert response.status_code == 409
    assert without_request_id(response.json()) == {
        "error": {"code": "conflict", "message": "An account with these details already exists"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["0712345678", "+254712345678", "FARMER@Example.COM"])
async def test_login_accepts_phone_formats_and_case_insensitive_email(api_client, application, identifier) -> None:
    user = make_user()
    application.dependency_overrides[get_db] = make_session_override(FakeSession(user))
    auth_service.get_login_throttle().reset()
    response = await api_client.post("/api/v1/auth/login", json={"identifier": identifier, "password": "password123"})
    assert response.status_code == 200
    assert response.json()["role"] == "farmer"
    assert response.json()["expires_in"] > 0
    assert "password_hash" not in response.json()


@pytest.mark.asyncio
async def test_login_failures_have_identical_responses(api_client, application) -> None:
    expected = {"error": {"code": "invalid_credentials", "message": "Invalid identifier or password"}}
    auth_service.get_login_throttle().reset()
    responses = []
    for user, identifier, password in [
        (None, "missing@example.com", "password123"),
        (make_user(), "farmer@example.com", "wrong-password"),
        (make_user(is_disabled=True), "farmer@example.com", "password123"),
    ]:
        application.dependency_overrides[get_db] = make_session_override(FakeSession(user))
        response = await api_client.post("/api/v1/auth/login", json={"identifier": identifier, "password": password})
        responses.append((response.status_code, without_request_id(response.json())))
    assert responses == [(401, expected)] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER])
async def test_non_admin_cannot_access_admin_routes(api_client, application, role) -> None:
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role=role, is_super_admin=False, must_change_password=False, is_disabled=False
    )
    response = await api_client.get("/api/v1/admin/stats")
    assert response.status_code == 403
