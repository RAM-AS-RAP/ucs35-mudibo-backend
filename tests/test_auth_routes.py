"""Route tests for fixed public registration roles and authorization."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.dependencies import get_current_user
from app.db import get_db
from app.models.enums import UserRole
from app.services import auth_service


async def fake_db():
    yield object()


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

    async def register(session, role, fields):
        called["role"] = role
        return SimpleNamespace(
            id=uuid4(),
            name=fields["name"],
            phone_number=fields["phone_number"],
            email=None,
            role=role,
            must_change_password=False,
            is_disabled=False,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(auth_service, "register_user", register)
    response = await api_client.post(
        "/api/v1/auth/register/farmer", json={"name": "Farmer", "phone_number": "0712345678", "password": "password123"}
    )
    assert response.status_code == 201
    assert called["role"] == UserRole.FARMER
    assert response.json()["role"] == "farmer"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER])
async def test_non_admin_cannot_access_admin_routes(api_client, application, role) -> None:
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role=role, is_super_admin=False, must_change_password=False, is_disabled=False
    )
    response = await api_client.get("/api/v1/admin/stats")
    assert response.status_code == 403
