"""Registration schema hardening and non-echoing validation responses."""

import pytest

from app.db import get_db
from app.schemas.auth import ChangePasswordRequest


async def fake_db():
    yield object()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["farmer", "buyer"])
@pytest.mark.parametrize("extra", ["role", "is_super_admin", "is_disabled", "must_change_password"])
async def test_registration_rejects_privileged_fields(api_client, application, path, extra) -> None:
    application.dependency_overrides[get_db] = fake_db
    body = {"name": "Ada Farmer", "phone_number": "0712345678", "password": "Secret123", extra: True}
    response = await api_client.post(f"/api/v1/auth/register/{path}", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert extra in response.json()["error"]["fields"]
    assert "Secret123" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["short77", "x" * 129])
async def test_registration_rejects_password_length_without_echo(api_client, application, password) -> None:
    application.dependency_overrides[get_db] = fake_db
    response = await api_client.post(
        "/api/v1/auth/register/farmer",
        json={"name": "Ada Farmer", "phone_number": "0712345678", "password": password},
    )
    assert response.status_code == 422
    assert password not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("password", "missing_requirement"),
    [
        ("lowercase7!", "uppercase letter"),
        ("UPPERCASE7!", "lowercase letter"),
        ("NoDigitsHere!", "digit"),
        ("NoSymbol123", "symbol"),
        ("Aa1!", "at least 8 characters"),
        ("Aa1!" + "x" * 126, "no more than 128 characters"),
    ],
)
async def test_registration_reports_missing_password_requirements(
    api_client, application, password, missing_requirement
) -> None:
    application.dependency_overrides[get_db] = fake_db
    response = await api_client.post(
        "/api/v1/auth/register/farmer",
        json={"name": "Ada Farmer", "phone_number": "0712345678", "password": password},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["fields"] == ["password"]
    assert missing_requirement in error["details"][0]["message"]
    assert password not in response.text


def test_change_password_rejects_unchanged_password() -> None:
    with pytest.raises(ValueError):
        ChangePasswordRequest(current_password="same-password", new_password="same-password")
