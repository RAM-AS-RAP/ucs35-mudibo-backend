"""Database tests for migration 20261026000100_identity_v2 and the code flows that use it.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a migrated, disposable local database. All
tests except the concurrency test run inside the rollback-only ``db_session`` fixture; inside it the
whole test is one transaction, so ``now()`` does not move and times are shifted with explicit updates.

The concurrency test needs two real connections, so it commits: it leaves one disabled ``ZZ_TEST_``
user behind (users cannot be deleted) and deletes its pending registration rows.
"""

import asyncio
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from invite_fakes import FakeMailer, configure_codes
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import hash_token
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import decode_access_token, hash_password
from app.db import get_engine, get_sessionmaker
from app.integrations import email as email_integration
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services import auth_service, registration_service

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261026000100_identity_v2.sql"
PASSWORD = "Mavuno-2026!x"


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    settings = configure_codes(monkeypatch, get_settings())
    registration_service.get_exists_notice_throttle().reset()
    auth_service.get_login_throttle().reset()
    return settings


@pytest.fixture
def mailer(monkeypatch):
    fake = FakeMailer()
    monkeypatch.setattr(email_integration, "send_email", fake.send_email)
    return fake


def random_email(prefix: str = "zz-test") -> str:
    return f"{prefix}-{secrets.token_hex(4)}@example.com"


async def make_user(session: AsyncSession, role: UserRole = UserRole.FARMER, **values) -> User:
    values.setdefault("name", f"ZZ_TEST_ {role.value}")
    values.setdefault("email", random_email())
    values.setdefault("email_verified_at", func.now())
    values.setdefault("password_hash", "not-a-real-hash")
    user = User(role=role, **values)
    session.add(user)
    await session.commit()
    return user


async def expect_database_error(session: AsyncSession, sql: str, params: dict | None = None) -> str:
    """Run ``sql`` in a savepoint that is rolled back when the database rejects it; return the message."""
    with pytest.raises(DBAPIError) as exc_info:
        async with session.begin_nested():
            await session.execute(text(sql), params or {})
    return str(exc_info.value)


async def insert_pending(session: AsyncSession, email: str, **overrides) -> UUID:
    values = {
        "email": email,
        "name": "Pending",
        "role": "farmer",
        "token_hash": secrets.token_hex(32),
        "code_hash": "code",
    }
    values.update(overrides)
    result = await session.execute(
        text(
            "insert into public.pending_registrations"
            " (email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at)"
            " values (:email, :name, cast(:role as public.user_role), 'hash', :token_hash, :code_hash,"
            "         now() + interval '15 minutes', now() + interval '60 minutes')"
            " returning id"
        ),
        values,
    )
    return result.scalar_one()


async def insert_challenge(session: AsyncSession, user_id: UUID) -> UUID:
    result = await session.execute(
        text(
            "insert into public.login_challenges (user_id, token_hash, code_hash, code_expires_at, expires_at)"
            " values (:user_id, :token_hash, 'code', now() + interval '10 minutes', now() + interval '10 minutes')"
            " returning id"
        ),
        {"user_id": user_id, "token_hash": secrets.token_hex(32)},
    )
    return result.scalar_one()


# Migration guard ---------------------------------------------------------------------------------


def guard_block() -> str:
    """The first ``do $$ ... $$;`` block of the migration: the guard."""
    match = re.search(r"do \$\$.*?\$\$;", MIGRATION.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert match is not None
    return match.group(0)


async def test_the_guard_refuses_a_database_with_users(db_session: AsyncSession) -> None:
    await make_user(db_session)
    message = await expect_database_error(db_session, guard_block())
    assert "identity v2 requires an empty users table" in message


# users -------------------------------------------------------------------------------------------


async def test_user_email_rules(db_session: AsyncSession) -> None:
    user = await make_user(db_session, email="zz-test-identity@example.com")
    insert = (
        "insert into public.users (name, email, password_hash, role, email_verified_at)"
        " values ('ZZ_TEST_ other', :email, 'hash', 'buyer', now())"
    )
    await expect_database_error(
        db_session,
        "insert into public.users (name, password_hash, role, email_verified_at) values ('x', 'h', 'buyer', now())",
    )
    await expect_database_error(db_session, insert, {"email": "Upper-Case@example.com"})
    await expect_database_error(db_session, insert, {"email": user.email})
    await expect_database_error(
        db_session,
        "insert into public.users (name, email, password_hash, role) values ('x', :email, 'h', 'buyer')",
        {"email": random_email()},
    )
    for change in ("email = 'zz-test-changed@example.com'", "email_verified_at = now() - interval '1 day'"):
        await expect_database_error(db_session, f"update public.users set {change} where id = :id", {"id": user.id})
    # Other columns still change normally.
    await db_session.execute(text("update public.users set name = 'ZZ_TEST_ renamed' where id = :id"), {"id": user.id})


async def test_phone_number_columns_are_gone(db_session: AsyncSession) -> None:
    count = await db_session.scalar(
        text(
            "select count(*) from information_schema.columns"
            " where table_schema = 'public' and table_name in ('users', 'admin_invites')"
            "   and column_name = 'phone_number'"
        )
    )
    assert count == 0


# pending_registrations ---------------------------------------------------------------------------


async def test_pending_registration_constraints_and_trigger(db_session: AsyncSession) -> None:
    email = random_email()
    pending_id = await insert_pending(db_session, email)
    insert = (
        "insert into public.pending_registrations"
        " (email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at)"
        " values (:email, 'x', cast(:role as public.user_role), 'h', :token, 'c', now(), now())"
    )
    await expect_database_error(db_session, insert, {"email": email, "role": "farmer", "token": secrets.token_hex(8)})
    await expect_database_error(
        db_session, insert, {"email": random_email(), "role": "admin", "token": secrets.token_hex(8)}
    )
    await expect_database_error(
        db_session, insert, {"email": "Upper@Example.com", "role": "buyer", "token": secrets.token_hex(8)}
    )
    update = "update public.pending_registrations set {} where id = :id"
    await expect_database_error(db_session, update.format("send_count = 6"), {"id": pending_id})
    await db_session.execute(text(update.format("attempts = 2")), {"id": pending_id})
    await expect_database_error(db_session, update.format("attempts = 1"), {"id": pending_id})
    for change in ("email = 'other@example.com'", "role = 'buyer'", "created_at = now() - interval '1 day'"):
        await expect_database_error(db_session, update.format(change), {"id": pending_id})
    # Allowed while pending.
    await db_session.execute(
        text(update.format("name = 'New', password_hash = 'h2', token_hash = :token, send_count = 2")),
        {"id": pending_id, "token": secrets.token_hex(8)},
    )
    await db_session.execute(text(update.format("used_at = now()")), {"id": pending_id})
    await expect_database_error(db_session, update.format("used_at = null"), {"id": pending_id})
    await expect_database_error(db_session, update.format("name = 'After use'"), {"id": pending_id})
    # A used row frees the email for a new open registration.
    await insert_pending(db_session, email)


# login_challenges --------------------------------------------------------------------------------


async def test_login_challenge_constraints_and_trigger(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    challenge_id = await insert_challenge(db_session, user.id)
    await expect_database_error(
        db_session,
        "insert into public.login_challenges (user_id, token_hash, code_hash, code_expires_at, expires_at)"
        " values (:user_id, 'other-token', 'c', now(), now())",
        {"user_id": user.id},
    )
    update = "update public.login_challenges set {} where id = :id"
    await expect_database_error(db_session, update.format("send_count = 6"), {"id": challenge_id})
    await db_session.execute(text(update.format("attempts = 3")), {"id": challenge_id})
    await expect_database_error(db_session, update.format("attempts = 0"), {"id": challenge_id})
    for change in ("token_hash = 'changed'", "user_id = gen_random_uuid()", "created_at = now() - interval '1 day'"):
        await expect_database_error(db_session, update.format(change), {"id": challenge_id})
    await db_session.execute(text(update.format("used_at = now()")), {"id": challenge_id})
    await expect_database_error(db_session, update.format("code_hash = 'changed'"), {"id": challenge_id})
    await insert_challenge(db_session, user.id)  # a used challenge frees the slot


# Security and jobs -------------------------------------------------------------------------------


async def test_row_level_security_privileges_and_cleanup_job(db_session: AsyncSession) -> None:
    for table in ("pending_registrations", "login_challenges"):
        enabled = await db_session.scalar(
            text("select relrowsecurity from pg_class where oid = cast(:name as regclass)"), {"name": f"public.{table}"}
        )
        assert enabled is True, table
        for role in ("anon", "authenticated"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                allowed = await db_session.scalar(
                    text(
                        "select has_table_privilege(cast(:role as name), cast(:name as text), cast(:privilege as text))"
                    ),
                    {"role": role, "name": f"public.{table}", "privilege": privilege},
                )
                assert allowed is False, (role, table, privilege)
    jobs = await db_session.scalar(text("select count(*) from cron.job where jobname = 'cleanup-auth-codes'"))
    assert jobs == 1


async def test_the_cleanup_removes_only_old_rows(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    old_pending = await insert_pending(db_session, random_email())
    await db_session.execute(
        text("update public.pending_registrations set used_at = now() where id = :id"), {"id": old_pending}
    )
    fresh_pending = await insert_pending(db_session, random_email())
    # created_at is immutable through updates, so the old row is inserted with an old created_at.
    old_challenge = (
        await db_session.execute(
            text(
                "insert into public.login_challenges"
                " (user_id, token_hash, code_hash, code_expires_at, expires_at, created_at, used_at)"
                " values (:user_id, :token, 'c', now(), now(), now() - interval '3 days', now())"
                " returning id"
            ),
            {"user_id": user.id, "token": secrets.token_hex(16)},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "insert into public.pending_registrations"
            " (email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at,"
            "  created_at, used_at)"
            " values (:email, 'Old', 'buyer', 'h', :token, 'c', now(), now(), now() - interval '3 days', now())"
        ),
        {"email": random_email(), "token": secrets.token_hex(16)},
    )
    await db_session.scalar(text("select public.cleanup_auth_codes()"))
    remaining = set((await db_session.execute(text("select id from public.pending_registrations"))).scalars().all())
    assert {old_pending, fresh_pending} <= remaining
    gone = await db_session.scalar(
        text("select count(*) from public.login_challenges where id = :id"), {"id": old_challenge}
    )
    assert gone == 0


# Registration flow -------------------------------------------------------------------------------


async def register(session: AsyncSession, email: str, mailer: FakeMailer, role=UserRole.FARMER):
    tasks = BackgroundTasks()
    body = RegisterRequest(name="ZZ_TEST_ registrant", email=email, password=PASSWORD)
    accepted = await registration_service.register(session, body, role, tasks)
    await tasks()
    return accepted


async def pending_row(session: AsyncSession, email: str):
    result = await session.execute(
        text("select * from public.pending_registrations where email = :email and used_at is null"), {"email": email}
    )
    return result.mappings().one()


async def test_registration_end_to_end(db_session: AsyncSession, mailer) -> None:
    email = random_email()
    accepted = await register(db_session, email, mailer)
    code = mailer.code()
    row = await pending_row(db_session, email)
    assert row["token_hash"] == hash_token(accepted.registration_token)
    assert code not in row["code_hash"] and PASSWORD not in row["password_hash"]
    result = await registration_service.verify(db_session, accepted.registration_token, code)
    user = await db_session.scalar(select(User).where(User.id == UUID(decode_access_token(result.access_token)["sub"])))
    assert (user.email, user.role, user.must_change_password) == (email, UserRole.FARMER, False)
    assert user.email_verified_at is not None and user.password_changed_at is not None
    with pytest.raises(AppError):
        await registration_service.verify(db_session, accepted.registration_token, code)


async def test_registration_attempts_lock_and_survive_resends(db_session: AsyncSession, mailer) -> None:
    email = random_email()
    accepted = await register(db_session, email, mailer)
    token = accepted.registration_token
    for _ in range(2):
        with pytest.raises(AppError):
            await registration_service.verify(db_session, token, "AAAA-AAAA")
    await db_session.execute(
        text("update public.pending_registrations set last_sent_at = now() - interval '2 minutes' where email = :e"),
        {"e": email},
    )
    tasks = BackgroundTasks()
    await registration_service.resend(db_session, token, tasks)
    await tasks()
    new_code = mailer.code()
    row = await pending_row(db_session, email)
    assert (row["attempts"], row["send_count"]) == (2, 2)
    for _ in range(3):
        with pytest.raises(AppError):
            await registration_service.verify(db_session, token, "AAAA-AAAA" if new_code != "AAAAAAAA" else "BBBB-BBBB")
    assert (await pending_row(db_session, email))["locked_at"] is not None
    with pytest.raises(AppError):
        await registration_service.verify(db_session, token, new_code)


async def test_an_email_taken_meanwhile_leaves_the_registration_unused(db_session: AsyncSession, mailer) -> None:
    email = random_email()
    accepted = await register(db_session, email, mailer)
    await make_user(db_session, UserRole.BUYER, email=email)
    with pytest.raises(AppError) as exc_info:
        await registration_service.verify(db_session, accepted.registration_token, mailer.code())
    assert exc_info.value.code == "invalid_or_expired_code"
    assert (await pending_row(db_session, email))["used_at"] is None


async def test_login_challenge_end_to_end(db_session: AsyncSession, mailer) -> None:
    user = await make_user(db_session, UserRole.BUYER, password_hash=hash_password(PASSWORD))
    first = await auth_service.login(db_session, user.email, PASSWORD)
    second = await auth_service.login(db_session, user.email, PASSWORD)
    active = await db_session.scalar(
        text("select count(*) from public.login_challenges where user_id = :id and used_at is null"), {"id": user.id}
    )
    assert active == 1
    with pytest.raises(AppError) as exc_info:
        await auth_service.verify_login(db_session, first.challenge_token, mailer.code(0))
    assert exc_info.value.code == "challenge_closed"
    result = await auth_service.verify_login(db_session, second.challenge_token, mailer.code())
    assert result.status == "signed_in" and result.role == "buyer"


# Concurrency (commits; see the module docstring) -------------------------------------------------


async def test_two_concurrent_verifications_create_exactly_one_user(mailer) -> None:
    factory = get_sessionmaker()
    email = random_email("zz-test-concurrent")
    async with factory() as session:
        accepted = await register(session, email, mailer)
    code = mailer.code()

    async def attempt() -> str:
        async with factory() as session:
            try:
                await registration_service.verify(session, accepted.registration_token, code)
            except AppError as exc:
                return exc.code
            return "created"

    try:
        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == ["created", "invalid_or_expired_code"]
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(User).where(User.email == email))
            assert count == 1
    finally:
        async with factory() as session:
            await session.execute(text("delete from public.pending_registrations where email = :e"), {"e": email})
            await session.execute(
                text("update public.users set is_disabled = true, disabled_at = now() where email = :e"), {"e": email}
            )
            await session.commit()
