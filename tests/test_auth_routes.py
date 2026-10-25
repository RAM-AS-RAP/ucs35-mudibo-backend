"""Route tests for fixed public registration roles, email sign in and authorization."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from response_helpers import without_request_id

from app.core.auth_result import signed_in_result
from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.security import hash_password
from app.db import get_db
from app.models.enums import UserRole
from app.schemas.auth import RegistrationAccepted
from app.services import auth_service, registration_service


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


def make_session_override(session):
    async def dependency():
        yield session

    return dependency


def make_user(role=UserRole.FARMER, **overrides):
    values = {
        "id": uuid4(),
        "name": "Farmer",
        "email": "farmer@example.com",
        "email_verified_at": datetime.now(UTC),
        "password_hash": hash_password("password123"),
        "role": role,
        "is_super_admin": False,
        "is_disabled": False,
        "must_change_password": False,
        "password_changed_at": None,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def password_only_sign_in(monkeypatch):
    """No role needs an emailed code, so a correct password signs in directly (code step tested elsewhere)."""
    monkeypatch.setattr(get_settings(), "login_otp_roles", "")


def accepted(email: str) -> RegistrationAccepted:
    return RegistrationAccepted(
        message=registration_service.ACCEPTED_MESSAGE,
        registration_token="t" * 43,
        email_masked=email[0] + "***",
        code_expires_at=datetime.now(UTC),
        resend_available_in=60,
    )


@pytest.mark.asyncio
async def test_registration_rejects_role_field(api_client, application) -> None:
    application.dependency_overrides[get_db] = fake_db
    response = await api_client.post(
        "/api/v1/auth/register/farmer",
        json={"name": "Farmer", "email": "farmer@example.com", "password": "Goodpass1!", "role": "admin"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "role"), [("farmer", UserRole.FARMER), ("buyer", UserRole.BUYER)])
async def test_registration_role_comes_from_the_route(api_client, application, monkeypatch, path, role) -> None:
    application.dependency_overrides[get_db] = fake_db
    called: dict[str, object] = {}

    async def register(session, body, route_role, background_tasks):
        called["role"] = route_role
        called["email"] = body.email
        return accepted(body.email)

    monkeypatch.setattr(registration_service, "register", register)
    body = {"name": "Farmer", "email": "Farmer@Example.com", "password": "Goodpass1!"}
    response = await api_client.post(f"/api/v1/auth/register/{path}", json=body)
    assert response.status_code == 202, response.text
    assert called == {"role": role, "email": "farmer@example.com"}
    assert set(response.json()) == {
        "message",
        "registration_token",
        "email_masked",
        "code_expires_at",
        "resend_available_in",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["FARMER@Example.COM", "farmer@example.com"])
async def test_login_is_by_case_insensitive_email(api_client, application, password_only_sign_in, email) -> None:
    user = make_user()
    application.dependency_overrides[get_db] = make_session_override(FakeSession(user))
    auth_service.get_login_throttle().reset()
    response = await api_client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "signed_in"
    assert response.json()["role"] == "farmer"
    assert response.json()["expires_in"] == get_settings().access_token_expire_minutes * 60
    assert "password_hash" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [{"identifier": "farmer@example.com", "password": "x"}, {"email": "0712345678", "password": "x"}]
)
async def test_login_needs_an_email(api_client, application, body) -> None:
    application.dependency_overrides[get_db] = fake_db
    response = await api_client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_login_failures_have_identical_responses(api_client, application) -> None:
    expected = {"error": {"code": "invalid_credentials", "message": "Invalid email or password"}}
    auth_service.get_login_throttle().reset()
    responses = []
    for user, email, password in [
        (None, "missing@example.com", "password123"),
        (make_user(), "farmer@example.com", "wrong-password"),
        (make_user(is_disabled=True), "farmer@example.com", "password123"),
    ]:
        application.dependency_overrides[get_db] = make_session_override(FakeSession(user))
        response = await api_client.post("/api/v1/auth/login", json={"email": email, "password": password})
        responses.append((response.status_code, without_request_id(response.json())))
    assert responses == [(401, expected)] * 3


@pytest.mark.asyncio
async def test_verification_routes_return_the_auth_result(api_client, application, monkeypatch) -> None:
    application.dependency_overrides[get_db] = fake_db
    user = make_user(UserRole.BUYER)

    async def verified(*args, **kwargs):
        return signed_in_result(user)

    monkeypatch.setattr(registration_service, "verify", verified)
    monkeypatch.setattr(auth_service, "verify_login", verified)
    register = await api_client.post(
        "/api/v1/auth/register/verify", json={"registration_token": "t" * 43, "code": "ABCD-EFGH"}
    )
    login = await api_client.post("/api/v1/auth/login/verify", json={"challenge_token": "t" * 43, "code": "ABCD-EFGH"})
    assert (register.status_code, login.status_code) == (201, 200)
    for response in (register, login):
        body = response.json()
        assert body["status"] == "signed_in" and body["role"] == "buyer"
        assert "challenge_token" not in body and "code_sent" not in body  # fields that do not apply are omitted


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER])
async def test_non_admin_cannot_access_admin_routes(api_client, application, role) -> None:
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role=role, is_super_admin=False, must_change_password=False, is_disabled=False
    )
    response = await api_client.get("/api/v1/admin/stats")
    assert response.status_code == 403
