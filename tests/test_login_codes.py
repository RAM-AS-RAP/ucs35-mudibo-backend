"""Two step sign in: password, then an emailed code bound to a challenge token. No database.

``AuthStore`` (``tests/auth_fakes.py``) stands in for ``users`` and ``login_challenges`` and the
database clock; ``FakeMailer`` records the emails so a test can read the code like a user would.
"""

import logging

import jwt
import pytest
from auth_fakes import AuthStore
from invite_fakes import FakeMailer, configure_codes
from pydantic import ValidationError
from response_helpers import without_request_id

from app.core.codes import format_code, generate_token, hash_code, hash_token
from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.integrations import email as email_integration
from app.integrations.email import EmailDeliveryError
from app.models.enums import UserRole
from app.routers.auth import limiter
from app.services import auth_service

PASSWORD = "Correct-Horse-9!"
INVALID_CREDENTIALS = {"error": {"code": "invalid_credentials", "message": "Invalid email or password"}}
CHALLENGE_CLOSED = {"error": {"code": "challenge_closed", "message": "Start again from the sign in screen."}}
WRONG_CODE = {"error": {"code": "invalid_or_expired_code", "message": "That code is incorrect or has expired."}}
SIGNED_IN_KEYS = {
    "status",
    "access_token",
    "token_type",
    "expires_in",
    "role",
    "must_change_password",
    "is_super_admin",
}
CODE_REQUIRED_KEYS = {
    "status",
    "challenge_token",
    "email_masked",
    "code_expires_at",
    "resend_available_in",
    "code_sent",
}
PRODUCTION = {
    "_env_file": None,
    "jwt_secret": "test-only-jwt-secret-at-least-thirty-two-characters",
    "app_env": "production",
    "email_provider": "resend",
    "email_api_key": "re_test_placeholder",
    "email_from_address": "noreply@agrisence.test",
    "frontend_base_url": "https://app.agrisence.test",
    "allowed_hosts": "api.agrisence.test",
    "cors_origins": "https://app.agrisence.test",
}


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    settings = configure_codes(monkeypatch, get_settings())
    auth_service.get_login_throttle().reset()
    yield settings
    auth_service.get_login_throttle().reset()


@pytest.fixture
def store(application) -> AuthStore:
    return AuthStore().install(application)


@pytest.fixture
def mailer(monkeypatch, store) -> FakeMailer:
    fake = FakeMailer(store)
    monkeypatch.setattr(email_integration, "send_email", fake.send_email)
    return fake


@pytest.fixture
def no_rate_limits(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)


async def login(api_client, email, password=PASSWORD):
    return await api_client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def verify(api_client, token, code):
    return await api_client.post("/api/v1/auth/login/verify", json={"challenge_token": token, "code": code})


async def resend(api_client, token):
    return await api_client.post("/api/v1/auth/login/resend", json={"challenge_token": token})


def other_code(code: str) -> str:
    return "AAAAAAAA" if code != "AAAAAAAA" else "BBBBBBBB"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ACCOUNTS = [
    pytest.param(UserRole.FARMER, False, id="farmer"),
    pytest.param(UserRole.BUYER, False, id="buyer"),
    pytest.param(UserRole.ADMIN, False, id="admin"),
    pytest.param(UserRole.ADMIN, True, id="super_admin"),
]


# Roles -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("role", "super_admin"), ACCOUNTS)
async def test_every_role_needs_a_code_by_default(api_client, store, mailer, role, super_admin):
    user = store.add_user(role, email=f"{role.value}@example.com", is_super_admin=super_admin)
    started = await login(api_client, user.email.upper())
    assert started.status_code == 200, started.text
    body = started.json()
    assert set(body) == CODE_REQUIRED_KEYS
    assert body["status"] == "code_required" and "access_token" not in body
    assert body["code_sent"] is True and body["resend_available_in"] == 60
    assert body["email_masked"] == f"{user.email[0]}***@e***.com"
    assert started.headers["cache-control"] == "no-store"
    assert len(mailer.sent) == 1 and mailer.sent[0].to == user.email
    signed_in = await verify(api_client, body["challenge_token"], mailer.code())
    assert signed_in.status_code == 200, signed_in.text
    result = signed_in.json()
    assert set(result) == SIGNED_IN_KEYS
    assert (result["status"], result["role"], result["is_super_admin"]) == ("signed_in", role.value, super_admin)
    claims = decode_access_token(result["access_token"])
    assert set(claims) == {"sub", "role", "iat", "exp"} and claims["sub"] == str(user.id)
    me = await api_client.get("/api/v1/auth/me", headers=bearer(result["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == str(user.id)


@pytest.mark.parametrize(
    ("role", "needs_code"), [(UserRole.FARMER, False), (UserRole.BUYER, False), (UserRole.ADMIN, True)]
)
async def test_roles_outside_login_otp_roles_get_a_token_directly(
    api_client, store, mailer, codes, monkeypatch, role, needs_code
):
    monkeypatch.setattr(codes, "login_otp_roles", "admin")
    user = store.add_user(role)
    response = await login(api_client, user.email)
    assert response.status_code == 200, response.text
    if needs_code:
        assert response.json()["status"] == "code_required" and len(mailer.sent) == 1
    else:
        assert response.json()["status"] == "signed_in" and set(response.json()) == SIGNED_IN_KEYS
        assert mailer.sent == [] and store.challenges == {}


# Password step ---------------------------------------------------------------------------------


async def test_wrong_password_unknown_email_and_disabled_account_look_the_same(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email="amina@example.com")
    store.add_user(UserRole.BUYER, email="off@example.com", is_disabled=True)
    responses = [
        await login(api_client, "amina@example.com", "Wrong-Horse-9!"),
        await login(api_client, "nobody@example.com"),
        await login(api_client, "off@example.com"),
    ]
    assert [(r.status_code, without_request_id(r.json())) for r in responses] == [(401, INVALID_CREDENTIALS)] * 3
    assert mailer.sent == [] and store.challenges == {}


async def test_a_correct_password_creates_one_challenge_and_stores_only_hashes(api_client, store, mailer):
    user = store.add_user(UserRole.FARMER)
    body = (await login(api_client, user.email)).json()
    (row,) = store.challenges.values()
    code = mailer.code()
    assert row["user_id"] == user.id
    assert row["token_hash"] == hash_token(body["challenge_token"]) and body["challenge_token"] not in str(row)
    assert row["code_hash"] == hash_code("login", row["id"], code) and code not in str(row)
    assert body["code_expires_at"].startswith(row["code_expires_at"].isoformat()[:19])
    message = mailer.sent[0]
    assert format_code(code) in message.text and "valid for 10 minutes" in message.text
    assert "If you did not just try to sign in, change your password." in message.text
    assert code not in message.subject and format_code(code) not in message.subject
    assert mailer.open_at_send == [[]]  # sent after the commit


async def test_a_failed_send_keeps_the_challenge_for_a_resend(api_client, store, mailer):
    user = store.add_user(UserRole.BUYER)
    mailer.error = EmailDeliveryError("connection_timeout")
    started = await login(api_client, user.email)
    assert started.status_code == 200
    assert started.json()["code_sent"] is False and started.json()["status"] == "code_required"
    mailer.error = None
    store.advance(seconds=61)
    resent = await resend(api_client, started.json()["challenge_token"])
    assert resent.status_code == 200 and resent.json()["code_sent"] is True
    assert (await verify(api_client, started.json()["challenge_token"], mailer.code())).status_code == 200


async def test_the_per_email_throttle_blocks_even_the_right_password(api_client, store, mailer, no_rate_limits):
    user = store.add_user(UserRole.FARMER, email="amina@example.com")
    for _ in range(5):
        assert (await login(api_client, user.email, "Wrong-Horse-9!")).status_code == 401
    blocked = await login(api_client, "AMINA@example.com")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) == 15 * 60
    assert mailer.sent == []


async def test_login_body_takes_email_and_password_only(api_client, store):
    for body in (
        {"identifier": "amina@example.com", "password": PASSWORD},
        {"email": "0712345678", "password": PASSWORD},
        {"email": "amina@example.com", "password": PASSWORD, "role": "admin"},
        {"email": "amina@example.com"},
    ):
        assert (await api_client.post("/api/v1/auth/login", json=body)).status_code == 422
    assert store.executed == []


# Code step -------------------------------------------------------------------------------------


async def test_the_code_alone_or_with_another_users_token_fails(api_client, store, mailer):
    amina = store.add_user(UserRole.FARMER, email="amina@example.com")
    otieno = store.add_user(UserRole.FARMER, email="otieno@example.com")
    amina_token = (await login(api_client, amina.email)).json()["challenge_token"]
    amina_code = mailer.code()
    otieno_token = (await login(api_client, otieno.email)).json()["challenge_token"]
    alone = await api_client.post("/api/v1/auth/login/verify", json={"code": amina_code})
    assert alone.status_code == 422
    crossed = await verify(api_client, otieno_token, amina_code)
    if amina_code != mailer.code():
        assert without_request_id(crossed.json()) == WRONG_CODE
    unknown = await verify(api_client, generate_token(), amina_code)
    assert without_request_id(unknown.json()) == CHALLENGE_CLOSED
    assert (await verify(api_client, amina_token, amina_code)).status_code == 200


async def test_wrong_codes_lock_the_challenge_then_it_is_closed(api_client, store, mailer, no_rate_limits):
    user = store.add_user(UserRole.ADMIN)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    code = mailer.code()
    for attempt in range(1, 6):
        failed = await verify(api_client, token, other_code(code))
        assert without_request_id(failed.json()) == WRONG_CODE, attempt
    (row,) = store.challenges.values()
    assert row["attempts"] == 5 and row["locked_at"] is not None
    closed = await verify(api_client, token, code)
    assert closed.status_code == 400
    assert without_request_id(closed.json()) == CHALLENGE_CLOSED


async def test_the_attempt_is_committed_although_the_request_fails(api_client, store, mailer):
    user = store.add_user(UserRole.BUYER)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    await verify(api_client, token, other_code(mailer.code()))
    (row,) = store.challenges.values()
    assert row["attempts"] == 1


async def test_resend_cooldown_cap_old_code_and_kept_attempts(api_client, store, mailer, no_rate_limits):
    user = store.add_user(UserRole.FARMER)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    first_code = mailer.code()
    early = await resend(api_client, token)
    assert early.status_code == 429
    assert early.json()["error"]["code"] == "resend_cooldown"
    assert early.headers["Retry-After"] == "60"
    store.advance(seconds=20)
    assert (await resend(api_client, token)).headers["Retry-After"] == "40"
    assert (await verify(api_client, token, other_code(first_code))).status_code == 400  # one attempt used
    store.advance(seconds=41)
    resent = await resend(api_client, token)
    assert resent.status_code == 200, resent.text
    assert set(resent.json()) == {"code_sent", "resend_available_in", "code_expires_at"}
    second_code = mailer.code()
    (row,) = store.challenges.values()
    assert row["attempts"] == 1 and row["send_count"] == 2  # attempts are not reset
    if second_code != first_code:
        assert without_request_id((await verify(api_client, token, first_code)).json()) == WRONG_CODE
    store.advance(seconds=61)
    assert (await resend(api_client, token)).status_code == 200  # the third and last send
    store.advance(seconds=61)
    capped = await resend(api_client, token)
    assert capped.status_code == 409 and capped.json()["error"]["code"] == "send_limit_reached"
    assert len(mailer.sent) == 3
    # The challenge (10 minutes) is still open, so the last code works.
    assert (await verify(api_client, token, mailer.code())).status_code == 200


async def test_resend_of_an_unknown_or_used_challenge_is_closed(api_client, store, mailer):
    user = store.add_user(UserRole.FARMER)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    assert (await verify(api_client, token, mailer.code())).status_code == 200
    for challenge in (token, generate_token()):
        response = await resend(api_client, challenge)
        assert without_request_id(response.json()) == CHALLENGE_CLOSED


async def test_a_new_login_supersedes_the_earlier_challenge(api_client, store, mailer):
    user = store.add_user(UserRole.BUYER)
    first = (await login(api_client, user.email)).json()["challenge_token"]
    first_code = mailer.code()
    second = (await login(api_client, user.email)).json()["challenge_token"]
    assert len(store.active_challenges(user.id)) == 1
    assert without_request_id((await verify(api_client, first, first_code)).json()) == CHALLENGE_CLOSED
    assert (await verify(api_client, second, mailer.code())).status_code == 200


async def test_the_hourly_challenge_limit(api_client, store, mailer, codes, monkeypatch, no_rate_limits):
    monkeypatch.setattr(codes, "login_challenges_per_hour", 2)
    user = store.add_user(UserRole.FARMER)
    assert (await login(api_client, user.email)).status_code == 200
    store.advance(minutes=10)
    assert (await login(api_client, user.email)).status_code == 200
    limited = await login(api_client, user.email)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "too_many_login_codes"
    assert limited.headers["Retry-After"] == str(50 * 60)
    assert len(mailer.sent) == 2
    store.advance(minutes=50, seconds=1)
    assert (await login(api_client, user.email)).json()["status"] == "code_required"


async def test_an_expired_code_or_challenge_is_closed(api_client, store, mailer):
    user = store.add_user(UserRole.FARMER)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    store.advance(minutes=10, seconds=1)
    assert without_request_id((await verify(api_client, token, mailer.code())).json()) == CHALLENGE_CLOSED
    assert without_request_id((await resend(api_client, token)).json()) == CHALLENGE_CLOSED


async def test_a_user_disabled_between_the_steps_gets_challenge_closed(api_client, store, mailer):
    user = store.add_user(UserRole.FARMER)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    user.is_disabled = True
    response = await verify(api_client, token, mailer.code())
    assert without_request_id(response.json()) == CHALLENGE_CLOSED


async def test_must_change_password_applies_after_verification(api_client, store, mailer):
    user = store.add_user(UserRole.ADMIN, must_change_password=True)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    result = (await verify(api_client, token, mailer.code())).json()
    assert result["must_change_password"] is True
    guarded = await api_client.get("/api/v1/admin/stats", headers=bearer(result["access_token"]))
    assert guarded.status_code == 403
    assert guarded.json()["error"]["code"] == "password_change_required"
    assert (await api_client.get("/api/v1/auth/me", headers=bearer(result["access_token"]))).status_code == 200


async def test_the_token_lifetime_follows_the_setting(api_client, store, mailer, codes, monkeypatch):
    assert Settings(_env_file=None, jwt_secret=PRODUCTION["jwt_secret"]).access_token_expire_minutes == 480
    monkeypatch.setattr(codes, "access_token_expire_minutes", 30)
    user = store.add_user(UserRole.FARMER)
    token = (await login(api_client, user.email)).json()["challenge_token"]
    result = (await verify(api_client, token, mailer.code())).json()
    assert result["expires_in"] == 1800
    claims = jwt.decode(result["access_token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 1800


def test_production_requires_admin_in_login_otp_roles():
    Settings(**PRODUCTION)  # the baseline is valid
    Settings(**PRODUCTION, login_otp_roles="admin")
    for roles in ("farmer,buyer", "", "farmer"):
        with pytest.raises(ValidationError):
            Settings(**PRODUCTION, login_otp_roles=roles)
    # Outside production a list without admin is allowed.
    development = Settings(_env_file=None, jwt_secret=PRODUCTION["jwt_secret"], login_otp_roles="farmer")
    assert development.login_otp_role_set == {"farmer"}


@pytest.mark.parametrize("roles", ["farmer,root", "admins", "farmer;buyer"])
def test_login_otp_roles_rejects_unknown_roles(roles):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret=PRODUCTION["jwt_secret"], login_otp_roles=roles)


# Secrets ---------------------------------------------------------------------------------------


async def test_no_code_token_password_or_full_email_is_logged_or_returned(api_client, store, mailer, caplog):
    caplog.set_level(logging.DEBUG)
    user = store.add_user(UserRole.ADMIN, email="grace.admin@example.com")
    wrong = await login(api_client, user.email, "Wrong-Horse-9!")
    started = await login(api_client, user.email)
    token = started.json()["challenge_token"]
    code = mailer.code()
    failed = await verify(api_client, token, other_code(code))
    signed_in = await verify(api_client, token, format_code(code))
    access_token = signed_in.json()["access_token"]
    secrets = (code, format_code(code), token, access_token, PASSWORD, "Wrong-Horse-9!", user.email)
    log_text = caplog.text + "\n".join(str(record.args) for record in caplog.records)
    for secret in secrets:
        assert secret not in log_text, secret
    for response in (wrong, started, failed):
        for secret in (code, format_code(code), PASSWORD, user.email):
            assert secret not in response.text
    assert code not in signed_in.text and PASSWORD not in signed_in.text


async def test_without_a_pepper_code_routes_are_503(api_client, store, mailer, codes, monkeypatch):
    user = store.add_user(UserRole.FARMER)
    monkeypatch.setattr(codes, "invite_code_pepper", "")
    responses = [
        await login(api_client, user.email),
        await verify(api_client, generate_token(), "ABCD-EFGH"),
        await resend(api_client, generate_token()),
    ]
    assert {(r.status_code, r.json()["error"]["code"]) for r in responses} == {(503, "codes_unavailable")}
    assert mailer.sent == []
