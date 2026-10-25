"""Farmer and buyer registration confirmed by an emailed code, without a database.

``AuthStore`` (``tests/auth_fakes.py``) stands in for the three tables and the database clock, so
these tests drive whole flows through the API: register, read the code from the fake mailer, verify,
and use the returned token. ``test_identity_v2_db.py`` checks the same rules against Postgres.
"""

import asyncio
import logging

import pytest
from auth_fakes import AuthStore
from invite_fakes import FakeMailer, configure_codes
from response_helpers import without_request_id

from app.core.codes import format_code, generate_token, hash_code, hash_token
from app.core.config import get_settings
from app.core.security import verify_password
from app.integrations import email as email_integration
from app.models.enums import UserRole
from app.routers.auth import limiter
from app.services import registration_service

PASSWORD = "Mavuno-2026!x"
FARMER = {"name": "Amina Farmer", "email": "Amina@Example.com", "password": PASSWORD}
EMAIL = "amina@example.com"
ACCEPTED_KEYS = {"message", "registration_token", "email_masked", "code_expires_at", "resend_available_in"}
ACCEPTED_MESSAGE = "If this address can be registered, we have sent a code to it."
GENERIC_FAILURE = {
    "error": {
        "code": "invalid_or_expired_code",
        "message": "That code is incorrect or has expired. Request a new code or start again.",
    }
}
RESEND_BODY = {"message": ACCEPTED_MESSAGE, "resend_available_in": 60}


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    settings = configure_codes(monkeypatch, get_settings())
    registration_service.get_exists_notice_throttle().reset()
    yield settings
    registration_service.get_exists_notice_throttle().reset()


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


async def register(api_client, body=FARMER, role="farmer"):
    return await api_client.post(f"/api/v1/auth/register/{role}", json=body)


async def verify(api_client, token, code):
    return await api_client.post("/api/v1/auth/register/verify", json={"registration_token": token, "code": code})


async def resend(api_client, token):
    return await api_client.post("/api/v1/auth/register/resend", json={"registration_token": token})


def other_code(code: str) -> str:
    return "AAAAAAAA" if code != "AAAAAAAA" else "BBBBBBBB"


# Response shape and enumeration ----------------------------------------------------------------


async def test_new_and_existing_emails_get_the_same_202_shape(api_client, store, mailer):
    store.add_user(UserRole.BUYER, email="taken@example.com")
    new = await register(api_client)
    existing = await register(api_client, {**FARMER, "email": "Taken@Example.com"})
    assert new.status_code == existing.status_code == 202
    assert set(new.json()) == set(existing.json()) == ACCEPTED_KEYS
    assert new.json()["message"] == existing.json()["message"] == ACCEPTED_MESSAGE
    assert new.json()["resend_available_in"] == existing.json()["resend_available_in"] == 60
    assert new.json()["email_masked"] == "a***@e***.com"
    assert new.headers["cache-control"] == "no-store"


async def test_an_existing_email_gets_the_notice_once_and_never_a_code(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email="taken@example.com")
    first = await register(api_client, {**FARMER, "email": "taken@example.com"})
    second = await register(api_client, {**FARMER, "email": "taken@example.com"})
    assert first.status_code == second.status_code == 202
    assert len(mailer.sent) == 1
    notice = mailer.sent[0]
    assert notice.to == "taken@example.com"
    assert "an account already exists" in notice.text
    assert "Forgot password" in notice.text
    assert registration_service.ACCEPTED_MESSAGE not in notice.text
    assert not any(character.isdigit() for character in notice.text)  # no code of any kind
    assert store.pending == {}  # nothing is stored for an existing address
    assert first.json()["registration_token"] != second.json()["registration_token"]


async def test_the_notice_is_sent_again_after_the_window(api_client, store, mailer, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(registration_service.get_exists_notice_throttle(), "clock", lambda: clock[0])
    store.add_user(UserRole.FARMER, email="taken@example.com")
    await register(api_client, {**FARMER, "email": "taken@example.com"})
    clock[0] += 10 * 60 + 1
    await register(api_client, {**FARMER, "email": "taken@example.com"})
    assert len(mailer.sent) == 2


async def test_a_dummy_token_never_verifies(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email="taken@example.com")
    response = await register(api_client, {**FARMER, "email": "taken@example.com"})
    for code in ("ABCD-EFGH", "AAAA-AAAA"):
        failed = await verify(api_client, response.json()["registration_token"], code)
        assert failed.status_code == 400
        assert without_request_id(failed.json()) == GENERIC_FAILURE
    assert len(store.users) == 1


# Storage ---------------------------------------------------------------------------------------


async def test_a_new_email_stores_only_hashes_and_emails_the_code(api_client, store, mailer, caplog):
    caplog.set_level(logging.DEBUG)
    response = await register(api_client)
    assert response.status_code == 202, response.text
    token = response.json()["registration_token"]
    (row,) = store.pending.values()
    code = mailer.code()
    assert row["email"] == EMAIL and row["role"] == "farmer" and row["name"] == "Amina Farmer"
    assert row["password_hash"] != PASSWORD and verify_password(row["password_hash"], PASSWORD)
    assert row["token_hash"] == hash_token(token) and token not in str(row)
    assert row["code_hash"] == hash_code("register", row["id"], code) and code not in str(row)
    assert row["send_count"] == 1 and row["attempts"] == 0
    message = mailer.sent[0]
    assert message.to == EMAIL
    assert code not in message.subject and format_code(code) not in message.subject
    assert format_code(code) in message.text and "valid for 15 minutes" in message.text
    assert "If you did not try to register, ignore this email." in message.text
    for secret in (code, format_code(code), token, PASSWORD, EMAIL):
        assert secret not in caplog.text
    assert code not in response.text
    # Sent after the commit, with no transaction open.
    assert mailer.open_at_send == [[]]


async def test_resubmitting_inside_the_cooldown_rotates_the_token_and_keeps_the_code(api_client, store, mailer):
    first = await register(api_client)
    code = mailer.code()
    second = await register(api_client, {**FARMER, "name": "Amina W. Farmer"})
    assert second.status_code == 202
    assert len(mailer.sent) == 1  # no second email
    assert first.json()["registration_token"] != second.json()["registration_token"]
    assert 0 < second.json()["resend_available_in"] <= 60
    (row,) = store.pending.values()
    assert row["send_count"] == 1 and row["name"] == "Amina W. Farmer"
    stale = await verify(api_client, first.json()["registration_token"], code)
    assert without_request_id(stale.json()) == GENERIC_FAILURE
    created = await verify(api_client, second.json()["registration_token"], code)
    assert created.status_code == 201, created.text
    assert store.user_by_email(EMAIL).name == "Amina W. Farmer"


async def test_after_the_cooldown_a_new_code_replaces_the_old_one(api_client, store, mailer):
    await register(api_client)
    old_code = mailer.code()
    store.advance(seconds=61)
    second = await register(api_client)
    assert len(mailer.sent) == 2
    new_code = mailer.code()
    (row,) = store.pending.values()
    assert row["send_count"] == 2
    token = second.json()["registration_token"]
    if new_code != old_code:
        assert (await verify(api_client, token, old_code)).status_code == 400
    assert (await verify(api_client, token, new_code)).status_code == 201


async def test_the_send_cap_keeps_the_last_code_and_sends_nothing(api_client, store, mailer, codes, monkeypatch):
    monkeypatch.setattr(codes, "reg_max_sends", 2)
    await register(api_client)
    store.advance(seconds=61)
    await register(api_client)
    last_code = mailer.code()
    store.advance(seconds=61)
    capped = await register(api_client)
    assert capped.status_code == 202 and set(capped.json()) == ACCEPTED_KEYS
    assert len(mailer.sent) == 2
    store.advance(seconds=61)
    assert (await resend(api_client, capped.json()["registration_token"])).json() == RESEND_BODY
    assert len(mailer.sent) == 2
    assert (await verify(api_client, capped.json()["registration_token"], last_code)).status_code == 201


async def test_a_registration_for_the_other_role_replaces_the_row(api_client, store, mailer):
    await register(api_client, role="farmer")
    buyer = await register(api_client, role="buyer")
    assert len(store.open_pending(EMAIL)) == 1
    assert store.open_pending(EMAIL)[0]["role"] == "buyer"
    created = await verify(api_client, buyer.json()["registration_token"], mailer.code())
    assert created.json()["role"] == "buyer"


# Validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["farmer", "buyer"])
@pytest.mark.parametrize(
    "extra", [{"role": "admin"}, {"phone_number": "0712345678"}, {"is_super_admin": True}, {"is_disabled": False}]
)
async def test_unknown_fields_are_422_and_nothing_happens(api_client, store, mailer, path, extra):
    response = await register(api_client, {**FARMER, **extra}, role=path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert set(extra) <= set(response.json()["error"]["fields"])
    assert store.executed == [] and mailer.sent == []
    assert PASSWORD not in response.text


@pytest.mark.parametrize(("path", "role"), [("farmer", "farmer"), ("buyer", "buyer")])
async def test_the_role_comes_from_the_route(api_client, store, mailer, path, role):
    response = await register(api_client, role=path)
    (row,) = store.pending.values()
    assert row["role"] == role
    created = await verify(api_client, response.json()["registration_token"], mailer.code())
    assert created.json()["role"] == role
    assert store.user_by_email(EMAIL).role == UserRole(role)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({**FARMER, "password": "Short1!"}, "validation_error"),
        ({**FARMER, "password": "nouppercase7!"}, "validation_error"),
        ({**FARMER, "password": "P@ssw0rd123"}, "password_too_weak"),
        ({**FARMER, "name": "Wanjiru#2026Farm", "password": "Wanjiru#2026Farm"}, "password_too_weak"),
    ],
)
async def test_weak_passwords_are_422(api_client, store, mailer, body, code):
    response = await register(api_client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert store.executed == [] and mailer.sent == []
    assert body["password"] not in response.text


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b"])
async def test_an_invalid_email_is_422(api_client, store, email):
    response = await register(api_client, {**FARMER, "email": email})
    assert response.status_code == 422


# Verification ----------------------------------------------------------------------------------


async def test_the_correct_code_creates_a_verified_user_and_signs_in(api_client, store, mailer, codes):
    response = await register(api_client)
    created = await verify(api_client, response.json()["registration_token"], format_code(mailer.code()).lower())
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body["status"] == "signed_in"
    assert set(body) == {
        "status",
        "access_token",
        "token_type",
        "expires_in",
        "role",
        "must_change_password",
        "is_super_admin",
    }
    assert (body["role"], body["is_super_admin"], body["must_change_password"]) == ("farmer", False, False)
    assert body["expires_in"] == codes.access_token_expire_minutes * 60
    user = store.user_by_email(EMAIL)
    assert user.email_verified_at == store.now and user.password_changed_at == store.now
    assert verify_password(user.password_hash, PASSWORD)
    (row,) = store.pending.values()
    assert row["used_at"] == store.now
    me = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200, me.text
    assert me.json()["email"] == EMAIL and me.json()["role"] == "farmer"
    assert "phone_number" not in me.json()
    # The row is single use.
    again = await verify(api_client, response.json()["registration_token"], mailer.code())
    assert without_request_id(again.json()) == GENERIC_FAILURE


async def test_wrong_codes_count_across_resends_and_lock_the_row(api_client, store, mailer, no_rate_limits):
    response = await register(api_client)
    token = response.json()["registration_token"]
    first_code = mailer.code()
    for _ in range(2):
        assert (await verify(api_client, token, other_code(first_code))).status_code == 400
    store.advance(seconds=61)
    assert (await resend(api_client, token)).json() == RESEND_BODY
    new_code = mailer.code()
    (row,) = store.pending.values()
    assert row["attempts"] == 2  # a resend never resets the counter
    for _ in range(3):
        failed = await verify(api_client, token, other_code(new_code))
        assert without_request_id(failed.json()) == GENERIC_FAILURE
    assert row["attempts"] == 5 and row["locked_at"] is not None
    locked = await verify(api_client, token, new_code)
    assert without_request_id(locked.json()) == GENERIC_FAILURE
    assert store.users == {}


async def test_the_attempt_is_committed_although_the_request_fails(api_client, store, mailer):
    response = await register(api_client)
    commits = store.commits
    failed = await verify(api_client, response.json()["registration_token"], other_code(mailer.code()))
    assert failed.status_code == 400
    (row,) = store.pending.values()
    # The request session was closed (uncommitted work undone) and the attempt is still there.
    assert row["attempts"] == 1
    assert store.commits == commits + 1


async def test_two_concurrent_verifications_create_exactly_one_user(api_client, store, mailer):
    response = await register(api_client)
    token, code = response.json()["registration_token"], mailer.code()
    results = await asyncio.gather(verify(api_client, token, code), verify(api_client, token, code))
    assert sorted(result.status_code for result in results) == [201, 400]
    assert len(store.users) == 1


async def test_an_email_created_meanwhile_gives_the_generic_failure_and_keeps_the_row(api_client, store, mailer):
    response = await register(api_client)
    store.before_user_insert = lambda s: s.add_user(UserRole.BUYER, email=EMAIL)
    failed = await verify(api_client, response.json()["registration_token"], mailer.code())
    assert failed.status_code == 400
    assert without_request_id(failed.json()) == GENERIC_FAILURE
    (row,) = store.pending.values()
    assert row["used_at"] is None  # the claim was rolled back with the insert
    assert len(store.users) == 1 and store.user_by_email(EMAIL).role == UserRole.BUYER


async def test_an_expired_code_fails_and_a_resend_gives_a_working_one(api_client, store, mailer):
    response = await register(api_client)
    token = response.json()["registration_token"]
    store.advance(minutes=15, seconds=1)
    expired = await verify(api_client, token, mailer.code())
    assert without_request_id(expired.json()) == GENERIC_FAILURE
    assert (await resend(api_client, token)).json() == RESEND_BODY
    assert len(mailer.sent) == 2
    assert (await verify(api_client, token, mailer.code())).status_code == 201


async def test_an_expired_registration_is_closed(api_client, store, mailer):
    response = await register(api_client)
    token = response.json()["registration_token"]
    store.advance(minutes=60, seconds=1)
    assert (await resend(api_client, token)).json() == RESEND_BODY
    assert len(mailer.sent) == 1  # nothing sent for an expired registration
    assert without_request_id((await verify(api_client, token, mailer.code())).json()) == GENERIC_FAILURE
    # Registering again starts a new row; the old one is marked used.
    await register(api_client)
    assert len(store.open_pending(EMAIL)) == 1 and len(store.pending) == 2


# Resend ----------------------------------------------------------------------------------------


async def test_resend_answers_the_same_for_real_dummy_and_unknown_tokens(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email="taken@example.com")
    real = (await register(api_client)).json()["registration_token"]
    dummy = (await register(api_client, {**FARMER, "email": "taken@example.com"})).json()["registration_token"]
    store.advance(seconds=61)
    responses = [await resend(api_client, token) for token in (real, dummy, generate_token())]
    assert [response.status_code for response in responses] == [202, 202, 202]
    assert [response.json() for response in responses] == [RESEND_BODY] * 3
    # Only the real registration got a new code (plus the first code and the exists notice).
    assert [message.to for message in mailer.sent] == [EMAIL, "taken@example.com", EMAIL]


async def test_resend_inside_the_cooldown_sends_nothing(api_client, store, mailer):
    token = (await register(api_client)).json()["registration_token"]
    assert (await resend(api_client, token)).json() == RESEND_BODY
    assert len(mailer.sent) == 1
    (row,) = store.pending.values()
    assert row["send_count"] == 1


@pytest.mark.parametrize(
    "body", [{}, {"registration_token": "short"}, {"registration_token": generate_token(), "role": "admin"}]
)
async def test_resend_and_verify_bodies_are_validated(api_client, store, body):
    assert (await api_client.post("/api/v1/auth/register/resend", json=body)).status_code == 422
    verify_body = {**body, "code": "ABCD-EFGH"}
    assert (await api_client.post("/api/v1/auth/register/verify", json=verify_body)).status_code == 422
    assert store.executed == []


async def test_registration_without_a_pepper_is_503(api_client, store, codes, monkeypatch):
    monkeypatch.setattr(codes, "invite_code_pepper", "")
    response = await register(api_client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "codes_unavailable"
    assert store.executed == []
