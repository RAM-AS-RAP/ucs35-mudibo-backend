"""Google sign in through the API, without a database or network.

``AuthStore`` (``tests/auth_fakes.py``) stands in for ``users``, ``user_identities``, ``oauth_nonces``,
``google_signups`` and the database clock. The real ``GoogleVerifier`` checks tokens signed with the
test key (``tests/google_fakes.py``) against a JWKS document served by ``httpx.MockTransport``.
``test_google_db.py`` checks the database rules and the concurrent cases against Postgres.
"""

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from auth_fakes import AuthStore
from google_fakes import CLIENT_ID, EMAIL, SUBJECT, JwksServer, id_token, make_verifier
from invite_fakes import FakeMailer, RecordingDB, configure_codes, install, reset_attempt, rows
from response_helpers import without_request_id

from app.core.codes import format_code, generate_code, generate_token, hash_token
from app.core.config import get_settings
from app.core.security import decode_access_token, verify_password
from app.integrations import email as email_integration
from app.integrations.email_templates import account_exists_email
from app.integrations.google import get_google_verifier
from app.models.enums import UserRole
from app.routers.auth import limiter
from app.services import auth_service, listing_service, price_service, registration_service

GOOGLE_FAILED = {
    "error": {
        "code": "google_sign_in_failed",
        "message": "Google sign in did not work. Please try again or use your email and password.",
    }
}
NOT_ALLOWED = {
    "error": {"code": "google_not_allowed", "message": "Administrator accounts sign in with email and password."}
}
CONFLICT = {
    "error": {
        "code": "google_conflict",
        "message": "A different Google account is already connected to this email. "
        "Use that Google account, or sign in with your email and password.",
    }
}
RETRY = {"error": {"code": "google_retry", "message": "Please try signing in with Google again."}}
TICKET = {"error": {"code": "invalid_or_expired_ticket", "message": "Your Google sign in expired. Please try again."}}
INVALID_CREDENTIALS = {"error": {"code": "invalid_credentials", "message": "Invalid email or password"}}
SIGNED_IN_KEYS = {
    "status",
    "access_token",
    "token_type",
    "expires_in",
    "role",
    "must_change_password",
    "is_super_admin",
}
ROLE_REQUIRED_KEYS = {"status", "signup_token", "email", "name", "signup_expires_at"}
GOOGLE_SENTENCE = "If you usually sign in with Google, choose Continue with Google."
NEW_EMAIL = "new.person@example.com"
NEW_SUBJECT = "200000000000000000001"


@pytest.fixture(autouse=True)
def google_on(monkeypatch):
    settings = configure_codes(monkeypatch, get_settings())
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    auth_service.get_login_throttle().reset()
    registration_service.get_exists_notice_throttle().reset()
    yield settings
    auth_service.get_login_throttle().reset()
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
def jwks() -> JwksServer:
    return JwksServer()


@pytest.fixture(autouse=True)
def verifier(application, jwks, google_on):
    instance = make_verifier(jwks, google_on)
    application.dependency_overrides[get_google_verifier] = lambda: instance
    return instance


@pytest.fixture
def no_rate_limits(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)


@pytest.fixture
def every_role_needs_a_code(monkeypatch, google_on):
    monkeypatch.setattr(google_on, "login_otp_roles", "farmer,buyer,admin")


async def new_nonce(api_client) -> str:
    response = await api_client.post("/api/v1/auth/google/nonce")
    assert response.status_code == 200, response.text
    return response.json()["nonce"]


async def google(api_client, *, nonce: str | None = None, token_nonce: str | None = None, **claims):
    """Sign in with a fresh nonce (or ``nonce``); ``token_nonce`` puts a different nonce in the token."""
    nonce = nonce or await new_nonce(api_client)
    credential = id_token(token_nonce or nonce, **claims)
    return await api_client.post("/api/v1/auth/google", json={"credential": credential, "nonce": nonce})


async def complete(api_client, signup_token: str, role: str = "farmer"):
    return await api_client.post("/api/v1/auth/google/complete", json={"signup_token": signup_token, "role": role})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def new_google_account(api_client, role: str = "farmer", *, sub=NEW_SUBJECT, email=NEW_EMAIL) -> dict:
    started = await google(api_client, sub=sub, email=email, name="New Person")
    assert started.status_code == 200, started.text
    done = await complete(api_client, started.json()["signup_token"], role)
    assert done.status_code == 201, done.text
    return done.json()


# Nonce -----------------------------------------------------------------------------------------


async def test_nonces_are_unique_and_only_their_hash_is_stored(api_client, store):
    first = await api_client.post("/api/v1/auth/google/nonce")
    second = await api_client.post("/api/v1/auth/google/nonce")
    assert first.status_code == second.status_code == 200
    assert set(first.json()) == {"nonce", "expires_in"} and first.json()["expires_in"] == 300
    assert first.headers["cache-control"] == "no-store"
    nonces = [first.json()["nonce"], second.json()["nonce"]]
    assert nonces[0] != nonces[1] and all(len(nonce) == 43 for nonce in nonces)
    stored = sorted(row["nonce_hash"] for row in store.nonces.values())
    assert stored == sorted(hash_token(nonce) for nonce in nonces)
    assert not any(nonce in str(store.nonces) for nonce in nonces)
    row = next(iter(store.nonces.values()))
    assert row["expires_at"] - row["created_at"] == timedelta(seconds=300)


async def test_a_nonce_is_single_use_so_a_replayed_token_fails(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email=EMAIL)
    nonce = await new_nonce(api_client)
    body = {"credential": id_token(nonce), "nonce": nonce}
    assert (await api_client.post("/api/v1/auth/google", json=body)).status_code == 200
    replay = await api_client.post("/api/v1/auth/google", json=body)
    assert (replay.status_code, without_request_id(replay.json())) == (401, GOOGLE_FAILED)


async def test_an_expired_nonce_fails(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email=EMAIL)
    nonce = await new_nonce(api_client)
    store.advance(seconds=301)
    response = await google(api_client, nonce=nonce)
    assert (response.status_code, without_request_id(response.json())) == (401, GOOGLE_FAILED)


async def test_every_failure_gives_the_same_401(api_client, store, mailer):
    store.add_user(UserRole.FARMER, email=EMAIL)
    issued = await new_nonce(api_client)
    never_issued = generate_token()
    responses = [
        # A nonce that was never issued.
        await google(api_client, nonce=never_issued),
        # The token carries another nonce than the request.
        await google(api_client, nonce=issued, token_nonce=generate_token()),
        # The token has no nonce claim at all.
        await api_client.post("/api/v1/auth/google", json={"credential": id_token(None), "nonce": issued}),
        # Verification failures.
        await api_client.post("/api/v1/auth/google", json={"credential": "x" * 40, "nonce": issued}),
        await google(api_client, nonce=issued, aud="999-other.apps.googleusercontent.com"),
        await google(api_client, nonce=issued, email_verified=False),
    ]
    assert [(r.status_code, without_request_id(r.json())) for r in responses] == [(401, GOOGLE_FAILED)] * 6
    # A mismatch is decided before the nonce is claimed, so the issued nonce is still usable.
    assert (await google(api_client, nonce=issued)).status_code == 200
    assert store.identities  # the farmer is now linked


async def test_google_routes_answer_503_without_a_client_id(api_client, store, jwks, google_on, monkeypatch):
    monkeypatch.setattr(google_on, "google_client_id", "")
    responses = [
        await api_client.post("/api/v1/auth/google/nonce"),
        await api_client.post("/api/v1/auth/google", json={"credential": id_token("n" * 43), "nonce": "n" * 43}),
        await complete(api_client, generate_token()),
    ]
    for response in responses:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "google_unavailable"
        assert response.headers["Retry-After"] == "5"
    assert store.executed == [] and jwks.calls == 0


async def test_a_jwks_outage_is_503_and_keeps_the_nonce(api_client, store, jwks):
    jwks.fail = True
    nonce = await new_nonce(api_client)
    response = await google(api_client, nonce=nonce)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "google_unavailable"
    assert response.headers["Retry-After"] == "5"
    assert all(row["used_at"] is None for row in store.nonces.values())


@pytest.mark.parametrize(
    "body",
    [
        {"credential": "x" * 8193, "nonce": "n" * 43},
        {"credential": "x" * 19, "nonce": "n" * 43},
        {"credential": "x" * 40, "nonce": "n" * 19},
        {"credential": "x" * 40, "nonce": "n" * 129},
        {"credential": "x" * 40},
        {"credential": "x" * 40, "nonce": "n" * 43, "role": "admin"},
    ],
)
async def test_bad_bodies_are_422_before_any_network_or_database_call(api_client, store, jwks, body):
    response = await api_client.post("/api/v1/auth/google", json=body)
    assert response.status_code == 422
    assert jwks.calls == 0 and store.executed == []
    assert "x" * 40 not in response.text


@pytest.mark.parametrize(
    ("path", "body", "limit", "status"),
    [
        ("/api/v1/auth/google/nonce", None, 20, 200),
        ("/api/v1/auth/google", {"credential": "x" * 40, "nonce": "n" * 43}, 10, 401),
        ("/api/v1/auth/google/complete", {"signup_token": "t" * 43, "role": "farmer"}, 10, 400),
    ],
)
async def test_google_routes_are_rate_limited_per_ip(api_client, store, path, body, limit, status):
    statuses = [(await api_client.post(path, json=body)).status_code for _ in range(limit + 1)]
    assert statuses == [status] * limit + [429]


# Sign in flows ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["farmer", "buyer"])
async def test_a_new_google_user_chooses_the_role_once(api_client, store, mailer, role):
    started = await google(api_client, sub=NEW_SUBJECT, email="New.Person@Example.com", name=" New\x00 Person ")
    assert started.status_code == 200, started.text
    body = started.json()
    assert set(body) == ROLE_REQUIRED_KEYS and body["status"] == "role_required"
    assert (body["email"], body["name"]) == (NEW_EMAIL, "New Person")
    assert started.headers["cache-control"] == "no-store"
    assert store.users == {}  # no account before the role is chosen
    (ticket,) = store.open_signups()
    assert ticket["token_hash"] == hash_token(body["signup_token"])
    assert body["signup_token"] not in str(store.signups)
    assert ticket["expires_at"] - ticket["created_at"] == timedelta(minutes=10)

    done = await complete(api_client, body["signup_token"], role)
    assert done.status_code == 201, done.text
    result = done.json()
    assert set(result) == SIGNED_IN_KEYS
    assert (result["status"], result["role"], result["is_super_admin"]) == ("signed_in", role, False)
    user = store.user_by_email(NEW_EMAIL)
    assert user.role == UserRole(role) and user.password_set is False and user.email_verified_at is not None
    assert not verify_password(user.password_hash, "")  # a random password nobody knows
    identity = store.identity_of(user.id)
    assert (identity["subject"], identity["email"], identity["name"]) == (NEW_SUBJECT, NEW_EMAIL, "New Person")

    me = await api_client.get("/api/v1/auth/me", headers=bearer(result["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["google_linked"] is True and me.json()["password_set"] is False
    assert me.json()["role"] == role and "sub" not in me.json() and NEW_SUBJECT not in me.text

    again = await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)
    assert again.status_code == 200 and set(again.json()) == SIGNED_IN_KEYS  # no role step any more
    assert again.json()["role"] == role
    assert len(store.users) == 1 and mailer.sent == []


async def test_an_existing_password_account_is_signed_in_and_linked(
    api_client, store, mailer, every_role_needs_a_code
):
    user = store.add_user(UserRole.BUYER, email=EMAIL)
    response = await google(api_client)
    assert response.status_code == 200, response.text
    assert set(response.json()) == SIGNED_IN_KEYS and response.json()["role"] == "buyer"
    assert mailer.sent == [] and store.challenges == {}  # Google sign in needs no emailed code
    identity = store.identity_of(user.id)
    assert identity["subject"] == SUBJECT and user.role == UserRole.BUYER
    me = (await api_client.get("/api/v1/auth/me", headers=bearer(response.json()["access_token"]))).json()
    assert (me["google_linked"], me["password_set"]) == (True, True)
    # Later sign ins follow the subject, even after the Google account's email changed.
    moved = await google(api_client, email="amina.moved@example.com")
    assert moved.status_code == 200 and moved.json()["role"] == "buyer"
    assert len(store.identities) == 1 and store.users == {user.id: user}


@pytest.mark.parametrize("super_admin", [False, True])
async def test_an_admin_email_is_refused_and_not_linked(api_client, store, mailer, super_admin):
    store.add_user(UserRole.ADMIN, email=EMAIL, is_super_admin=super_admin)
    response = await google(api_client)
    assert (response.status_code, without_request_id(response.json())) == (403, NOT_ALLOWED)
    assert store.identities == {} and store.signups == {}


async def test_an_admin_with_a_google_identity_is_refused(api_client, store):
    admin = store.add_user(UserRole.ADMIN, email=EMAIL)
    store.add_identity(admin, SUBJECT)
    response = await google(api_client)
    assert (response.status_code, without_request_id(response.json())) == (403, NOT_ALLOWED)


async def test_disabled_accounts_get_the_same_401_as_a_bad_token(api_client, store):
    store.add_user(UserRole.FARMER, email=EMAIL, is_disabled=True)
    by_email = await google(api_client)
    linked = store.add_user(UserRole.BUYER, email="linked@example.com", is_disabled=True)
    store.add_identity(linked, "300000000000000000003")
    by_identity = await google(api_client, sub="300000000000000000003", email="linked@example.com")
    bad_token = await google(api_client, aud="999-other.apps.googleusercontent.com")
    bodies = [(r.status_code, without_request_id(r.json())) for r in (by_email, by_identity, bad_token)]
    assert bodies == [(401, GOOGLE_FAILED)] * 3
    assert len(store.identities) == 1


async def test_another_google_account_for_a_linked_email_is_a_conflict(api_client, store):
    user = store.add_user(UserRole.FARMER, email=EMAIL)
    store.add_identity(user, SUBJECT)
    response = await google(api_client, sub="400000000000000000004")
    assert (response.status_code, without_request_id(response.json())) == (409, CONFLICT)
    assert [row["subject"] for row in store.identities.values()] == [SUBJECT]


async def test_a_repeated_sign_in_of_a_new_account_keeps_one_open_ticket(api_client, store):
    # Sequential here; test_google_db.py runs two sign ins concurrently against Postgres.
    first = (await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)).json()
    second = (await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)).json()
    assert first["status"] == second["status"] == "role_required"
    assert len(store.open_signups(NEW_SUBJECT)) == 1 and len(store.signups) == 2
    stale = await complete(api_client, first["signup_token"])
    assert (stale.status_code, without_request_id(stale.json())) == (400, TICKET)
    assert (await complete(api_client, second["signup_token"], "buyer")).status_code == 201


async def test_a_ticket_is_single_use_and_expires(api_client, store):
    token = (await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)).json()["signup_token"]
    assert (await complete(api_client, token)).status_code == 201
    replay = await complete(api_client, token)
    assert (replay.status_code, without_request_id(replay.json())) == (400, TICKET)
    assert len(store.users) == 1

    later = (await google(api_client, sub="500000000000000000005", email="later@example.com")).json()
    store.advance(minutes=10, seconds=1)
    expired = await complete(api_client, later["signup_token"])
    assert (expired.status_code, without_request_id(expired.json())) == (400, TICKET)
    unknown = await complete(api_client, generate_token())
    assert (unknown.status_code, without_request_id(unknown.json())) == (400, TICKET)
    assert len(store.users) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"role": "admin"},
        {"role": "superuser"},
        {"role": "Farmer"},
        {"role": "farmer", "is_super_admin": True},
        {"role": "farmer", "email": "other@example.com"},
        {},
    ],
)
async def test_complete_takes_only_farmer_or_buyer(api_client, store, body):
    token = (await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)).json()["signup_token"]
    executed = len(store.executed)
    response = await api_client.post("/api/v1/auth/google/complete", json={"signup_token": token, **body})
    assert response.status_code == 422
    assert len(store.executed) == executed and store.users == {}
    assert len(store.open_signups(NEW_SUBJECT)) == 1


async def test_an_account_created_meanwhile_rolls_back_the_ticket_claim(api_client, store):
    token = (await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)).json()["signup_token"]
    store.before_user_insert = lambda s: s.add_user(UserRole.BUYER, email=NEW_EMAIL)
    response = await complete(api_client, token, "farmer")
    assert (response.status_code, without_request_id(response.json())) == (409, RETRY)
    (ticket,) = store.signups.values()
    assert ticket["used_at"] is None and store.identities == {}  # the claim was rolled back
    # Signing in with Google again now finds the buyer account and links it.
    again = await google(api_client, sub=NEW_SUBJECT, email=NEW_EMAIL)
    assert again.status_code == 200 and again.json()["role"] == "buyer"


# Tokens and roles ------------------------------------------------------------------------------


async def test_google_tokens_work_like_password_tokens(api_client, store, monkeypatch, google_on):
    async def no_markets(session):
        return []

    monkeypatch.setattr(price_service, "markets", no_markets)
    monkeypatch.setattr(listing_service, "buyer_markets", no_markets)
    farmer = await new_google_account(api_client, "farmer")
    buyer = await new_google_account(api_client, "buyer", sub="600000000000000000006", email="buyer@example.com")
    for result in (farmer, buyer):
        claims = decode_access_token(result["access_token"])
        assert set(claims) == {"sub", "role", "iat", "exp"}
        assert result["expires_in"] == google_on.access_token_expire_minutes * 60
        assert claims["exp"] - claims["iat"] == result["expires_in"]
    farmer_auth, buyer_auth = bearer(farmer["access_token"]), bearer(buyer["access_token"])
    assert (await api_client.get("/api/v1/markets", headers=farmer_auth)).status_code == 200
    assert (await api_client.get("/api/v1/listings/markets", headers=buyer_auth)).status_code == 200
    assert (await api_client.get("/api/v1/markets", headers=buyer_auth)).status_code == 403
    assert (await api_client.get("/api/v1/listings/markets", headers=farmer_auth)).status_code == 403
    for headers in (farmer_auth, buyer_auth):
        response = await api_client.get("/api/v1/admin/stats", headers=headers)
        assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"


# Passwords -------------------------------------------------------------------------------------


async def test_a_google_only_account_fails_password_sign_in_like_a_wrong_password(api_client, store, mailer):
    await new_google_account(api_client)
    known = store.add_user(UserRole.BUYER, email="known@example.com", password="Known-Pass-9!", password_set=False)
    responses = [
        await api_client.post("/api/v1/auth/login", json={"email": NEW_EMAIL, "password": "Some-Pass-9!"}),
        # Even the right hash is refused while no password was set.
        await api_client.post("/api/v1/auth/login", json={"email": known.email, "password": "Known-Pass-9!"}),
        await api_client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "Some-Pass-9!"}),
    ]
    assert [(r.status_code, without_request_id(r.json())) for r in responses] == [(401, INVALID_CREDENTIALS)] * 3
    assert mailer.sent == [] and store.challenges == {}


async def test_registering_a_google_only_email_sends_the_exists_notice(api_client, store, mailer):
    await new_google_account(api_client)
    users = dict(store.users)
    response = await api_client.post(
        "/api/v1/auth/register/buyer", json={"name": "Someone", "email": NEW_EMAIL, "password": "Mavuno-2026!x"}
    )
    assert response.status_code == 202
    assert store.pending == {} and store.users == users
    (notice,) = mailer.sent
    assert notice.to == NEW_EMAIL and "an account already exists" in notice.text
    assert GOOGLE_SENTENCE in notice.text and GOOGLE_SENTENCE in notice.html


def test_the_exists_notice_mentions_google_in_both_versions():
    message = account_exists_email("amina@example.com")
    assert GOOGLE_SENTENCE in message.text and GOOGLE_SENTENCE in message.html
    assert "Forgot password" in message.text


async def test_change_password_without_a_password_is_409(api_client, store):
    result = await new_google_account(api_client)
    response = await api_client.post(
        "/api/v1/auth/change-password",
        headers=bearer(result["access_token"]),
        json={"current_password": "anything", "new_password": "Brand-New-Pass-9!"},
    )
    assert response.status_code == 409
    error = without_request_id(response.json())["error"]
    assert error == {
        "code": "password_not_set",
        "message": "You have not set a password yet. Use Forgot password to set one.",
    }


async def test_a_reset_sets_the_password_then_password_sign_in_works(
    api_client, application, mailer, no_rate_limits, every_role_needs_a_code
):
    new_password = "Shamba-Harvest-77"
    # The reset itself: the update sets password_set together with the new hash.
    code = generate_code()
    store = AuthStore()
    user = store.add_user(UserRole.FARMER, email="amina@example.com", password_set=False)
    store.add_identity(user, SUBJECT)
    script = [reset_attempt(code, user_id=user.id), rows({"user_id": user.id}), rows({"id": user.id})]
    db = install(application, None, RecordingDB(results=script))
    confirmed = await api_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"email": user.email, "code": format_code(code), "new_password": new_password},
    )
    assert confirmed.status_code == 204, confirmed.text
    update_sql = db.statements[db.index_of("update public.users")]
    assert "password_set = true" in update_sql and "password_changed_at = now()" in update_sql
    new_hash = db.params_for("update public.users")["password_hash"]

    # Apply what that statement does to the fake row, then sign in with the password and a code.
    settings = get_settings()
    issued = int(datetime.now(UTC).timestamp()) - 60
    old_token = jwt.encode(
        {"sub": str(user.id), "role": "farmer", "iat": issued, "exp": issued + 3600},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    user.password_hash, user.password_set, user.password_changed_at = new_hash, True, datetime.now(UTC)
    store.install(application)
    assert (await api_client.get("/api/v1/auth/me", headers=bearer(old_token))).status_code == 401
    mailer.db = store
    started = await api_client.post("/api/v1/auth/login", json={"email": user.email, "password": new_password})
    assert started.status_code == 200 and started.json()["status"] == "code_required"
    verified = await api_client.post(
        "/api/v1/auth/login/verify", json={"challenge_token": started.json()["challenge_token"], "code": mailer.code()}
    )
    assert verified.status_code == 200 and verified.json()["role"] == "farmer"
    me = (await api_client.get("/api/v1/auth/me", headers=bearer(verified.json()["access_token"]))).json()
    assert (me["password_set"], me["google_linked"]) == (True, True)


# Secrets ---------------------------------------------------------------------------------------


async def test_no_credential_nonce_ticket_or_subject_is_logged(api_client, store, caplog):
    caplog.set_level(logging.DEBUG)
    nonce = await new_nonce(api_client)
    credential = id_token(nonce, sub=NEW_SUBJECT, email=NEW_EMAIL)
    started = await api_client.post("/api/v1/auth/google", json={"credential": credential, "nonce": nonce})
    ticket = started.json()["signup_token"]
    done = await complete(api_client, ticket)
    replay = await complete(api_client, ticket)
    assert (done.status_code, replay.status_code) == (201, 400)
    log_text = caplog.text + "\n".join(str(record.args) for record in caplog.records)
    for secret in (credential, nonce, ticket, NEW_SUBJECT, NEW_EMAIL, done.json()["access_token"]):
        assert secret not in log_text
    for response in (started, done, replay):
        assert credential not in response.text and nonce not in response.text and NEW_SUBJECT not in response.text
