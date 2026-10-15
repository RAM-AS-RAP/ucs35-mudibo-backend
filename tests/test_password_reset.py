"""Password reset tests without a database: generic responses, attempt cap, policy and background sends.

Single use, expiry and one active code per user are enforced by SQL conditions and the partial unique
index; ``test_invites_db.py`` checks them against a real database.
"""

import logging
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from invite_fakes import NO_ROW, FakeMailer, RecordingDB, configure_codes, install, reset_attempt, rows
from payment_fakes import FakeDB

from app.core.codes import format_code, generate_code, hash_code
from app.core.config import get_settings
from app.core.errors import AppError, InvalidCredentials
from app.core.security import hash_password, verify_password
from app.integrations import email as email_integration
from app.models.enums import UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.invite import PasswordResetConfirmRequest
from app.services import auth_service, password_reset_service

GENERIC_FAILURE = {"error": {"code": "invalid_or_expired_code", "message": "The code is invalid or has expired."}}
REQUESTED = {"message": "If an account with this email exists, a reset code has been sent."}
NEW_PASSWORD = "Shamba-Harvest-77"
OLD_PASSWORD = "Old-passw0rd-2025"


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    return configure_codes(monkeypatch, get_settings())


@pytest.fixture
def mailer(monkeypatch):
    fake = FakeMailer()
    monkeypatch.setattr(email_integration, "send_email", fake.send_email)
    return fake


def known_user(user_id=None):
    return rows({"id": user_id or uuid4(), "email": "amina@example.com"})


def request_stats(recent=0, in_cooldown=False):
    return rows({"recent": recent, "in_cooldown": in_cooldown})


def confirm_body(code, new_password=NEW_PASSWORD, email="amina@example.com"):
    return {"email": email, "code": code, "new_password": new_password}


def request(body) -> PasswordResetConfirmRequest:
    return PasswordResetConfirmRequest(**body)


# Request -------------------------------------------------------------------------------------


async def test_request_answers_the_same_for_known_and_unknown_emails(api_client, application, mailer):
    install(application, None, RecordingDB(results=[known_user(), request_stats(), NO_ROW, NO_ROW]))
    known = await api_client.post("/api/v1/auth/password-reset/request", json={"email": "Amina@Example.com"})
    install(application, None, RecordingDB(results=[NO_ROW]))
    unknown = await api_client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert (known.status_code, known.json()) == (202, REQUESTED)
    assert (unknown.status_code, unknown.json()) == (202, REQUESTED)
    assert [message.to for message in mailer.sent] == ["amina@example.com"]


async def test_request_supersedes_the_active_code_and_sends_in_the_background(mailer, caplog):
    caplog.set_level(logging.DEBUG)
    user_id = uuid4()
    db = RecordingDB(results=[known_user(user_id), request_stats(), NO_ROW, NO_ROW])
    tasks = BackgroundTasks()

    await password_reset_service.request_reset(db.session(), "amina@example.com", tasks)

    assert "is_disabled = false" in db.statements[0] and "FOR UPDATE" in db.statements[0].upper()
    supersede = db.index_of("set used_at = now()")
    insert = db.index_of("insert into public.password_reset_codes")
    assert supersede < insert
    assert db.commits == 1
    assert mailer.sent == [] and len(tasks.tasks) == 1
    await tasks()
    code = mailer.code()
    stored = db.params[insert]
    assert stored["code_hash"] == hash_code("reset", stored["id"], code)
    assert code not in str(stored)
    message = mailer.sent[0]
    assert format_code(code) not in message.subject
    assert "https://admin.agrisence.test/reset-password" in message.text
    assert "If you did not expect this email, ignore it." in message.text
    assert code not in caplog.text


@pytest.mark.parametrize(("recent", "in_cooldown"), [(3, False), (0, True)])
async def test_request_respects_the_hourly_cap_and_the_cooldown(mailer, recent, in_cooldown):
    db = RecordingDB(results=[known_user(), request_stats(recent, in_cooldown)])
    tasks = BackgroundTasks()
    await password_reset_service.request_reset(db.session(), "amina@example.com", tasks)
    assert tasks.tasks == []
    assert not db.has("insert into public.password_reset_codes")
    assert db.commits == 1


async def test_request_for_an_unknown_or_disabled_account_does_nothing(mailer):
    db = RecordingDB(results=[NO_ROW])
    tasks = BackgroundTasks()
    await password_reset_service.request_reset(db.session(), "disabled@example.com", tasks)
    assert tasks.tasks == []
    assert len(db.statements) == 1


# Confirm -------------------------------------------------------------------------------------


def claimed(user_id):
    return rows({"user_id": user_id})


async def test_a_valid_code_sets_the_new_password(api_client, application, caplog):
    caplog.set_level(logging.DEBUG)
    code = generate_code()
    user_id = uuid4()
    db = install(
        application,
        None,
        RecordingDB(results=[reset_attempt(code, user_id=user_id), claimed(user_id), rows({"id": user_id})]),
    )

    response = await api_client.post("/api/v1/auth/password-reset/confirm", json=confirm_body(format_code(code)))

    assert response.status_code == 204, response.text
    update_sql = db.statements[db.index_of("update public.users")]
    assert "must_change_password = false" in update_sql and "password_changed_at = now()" in update_sql
    new_hash = db.params_for("update public.users")["password_hash"]
    assert verify_password(new_hash, NEW_PASSWORD)
    assert not verify_password(new_hash, OLD_PASSWORD)
    assert db.commits == 2
    assert code not in caplog.text and NEW_PASSWORD not in caplog.text

    user = User(
        id=user_id,
        name="Amina Farmer",
        phone_number="254712345678",
        email="amina@example.com",
        password_hash=new_hash,
        role=UserRole.FARMER,
        is_super_admin=False,
        must_change_password=False,
        is_disabled=False,
    )
    auth_service.get_login_throttle().reset()
    login = await auth_service.login(FakeDB(scalars=[user]).session(), "amina@example.com", NEW_PASSWORD)
    assert login["role"] == "farmer"
    with pytest.raises(InvalidCredentials):
        await auth_service.login(FakeDB(scalars=[user]).session(), "amina@example.com", OLD_PASSWORD)


async def test_step_a_only_counts_attempts_on_a_usable_code_of_an_enabled_user():
    db = RecordingDB(results=[NO_ROW])
    with pytest.raises(AppError):
        await password_reset_service.confirm_reset(db.session(), request(confirm_body("ABCD-EFGH")))
    sql = db.statements[0]
    for condition in (
        "attempts = c.attempts + 1",
        "u.is_disabled = false",
        "c.used_at is null",
        "c.locked_at is null",
        "c.code_expires_at > now()",
        "c.attempts < %(max_attempts)s",
    ):
        assert condition in sql
    assert db.params[0]["max_attempts"] == 5
    assert db.commits == 1


def reset_failures():
    code = generate_code()
    user_id = uuid4()
    other = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
    return {
        # Unknown email, disabled user, used, expired and locked codes leave step A without a row.
        "no_usable_code": ([NO_ROW], code),
        "wrong_code": ([reset_attempt(code, user_id=user_id)], other),
        "claimed_meanwhile": ([reset_attempt(code, user_id=user_id), NO_ROW], code),
        "disabled_meanwhile": ([reset_attempt(code, user_id=user_id), claimed(user_id), NO_ROW], code),
    }


async def test_every_reset_failure_returns_the_identical_400(api_client, application):
    bodies = []
    for name, (results, code) in reset_failures().items():
        limiter._storage.reset()
        db = install(application, None, RecordingDB(results=results))
        response = await api_client.post("/api/v1/auth/password-reset/confirm", json=confirm_body(code))
        assert response.status_code == 400, name
        bodies.append(response.json())
        assert db.commits >= 1, name
    assert bodies == [GENERIC_FAILURE] * len(bodies)


async def test_the_fifth_wrong_attempt_locks_the_code():
    code = generate_code()
    db = RecordingDB(results=[reset_attempt(code, attempts=5), NO_ROW])
    with pytest.raises(AppError):
        await password_reset_service.confirm_reset(db.session(), request(confirm_body("ZZZZ-ZZZZ")))
    assert db.index_of("set locked_at = now()") == 1
    assert db.commits == 1


async def test_a_wrong_attempt_below_the_cap_does_not_lock():
    code = generate_code()
    db = RecordingDB(results=[reset_attempt(code, attempts=4)])
    with pytest.raises(AppError):
        await password_reset_service.confirm_reset(db.session(), request(confirm_body("ZZZZ-ZZZZ")))
    assert not db.has("set locked_at")
    assert db.commits == 1


async def test_the_claim_requires_an_unused_unexpired_code():
    code = generate_code()
    user_id = uuid4()
    db = RecordingDB(results=[reset_attempt(code, user_id=user_id), claimed(user_id), rows({"id": user_id})])
    await password_reset_service.confirm_reset(db.session(), PasswordResetConfirmRequest(**confirm_body(code)))
    claim_sql = db.statements[1]
    assert "set used_at = now()" in claim_sql
    for condition in ("used_at is null", "locked_at is null", "code_expires_at > now()"):
        assert condition in claim_sql
    assert "is_disabled = false" in db.statements[2]


async def test_the_admin_policy_applies_after_the_code_for_admins():
    code = generate_code()
    db = RecordingDB(results=[reset_attempt(code, role="admin")])
    with pytest.raises(AppError) as exc_info:
        await password_reset_service.confirm_reset(
            db.session(), PasswordResetConfirmRequest(**confirm_body(code, new_password="Tenchars1!"))
        )
    assert (exc_info.value.code, exc_info.value.reason) == ("password_too_weak", "Use at least 12 characters.")
    assert not db.has("set used_at")


async def test_the_same_password_is_fine_for_a_farmer():
    code = generate_code()
    user_id = uuid4()
    db = RecordingDB(results=[reset_attempt(code, user_id=user_id), claimed(user_id), rows({"id": user_id})])
    await password_reset_service.confirm_reset(
        db.session(), PasswordResetConfirmRequest(**confirm_body(code, new_password="Tenchars1!"))
    )
    assert db.commits == 2


async def test_common_passwords_are_refused_before_the_code(api_client, application):
    db = install(application, None)
    response = await api_client.post(
        "/api/v1/auth/password-reset/confirm", json=confirm_body("ABCD-EFGH", new_password="password123")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "password_too_weak"
    assert db.statements == []


async def test_the_old_hash_is_never_reused():
    code = generate_code()
    user_id = uuid4()
    old_hash = hash_password(OLD_PASSWORD)
    db = RecordingDB(results=[reset_attempt(code, user_id=user_id), claimed(user_id), rows({"id": user_id})])
    await password_reset_service.confirm_reset(db.session(), PasswordResetConfirmRequest(**confirm_body(code)))
    assert db.params_for("update public.users")["password_hash"] != old_hash
