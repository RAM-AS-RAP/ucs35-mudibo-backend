"""Placeholder services must answer 501 instead of an unhandled 500."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.dependencies import get_current_user
from app.db import get_db
from app.models.enums import UserRole


async def fake_db():
    yield object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "path"),
    [(UserRole.FARMER, "/api/v1/listings"), (UserRole.ADMIN, "/api/v1/admin/stats")],
)
async def test_placeholder_service_returns_501(api_client, application, role, path) -> None:
    user = SimpleNamespace(id=uuid4(), role=role, is_super_admin=False, is_disabled=False, must_change_password=False)
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user] = lambda: user
    response = await api_client.get(path)
    assert response.status_code == 501
    assert response.json() == {"error": {"code": "not_implemented", "message": "This endpoint is not implemented yet"}}
