"""Database tests for migration 20261027000100_google_sign_in and the Google sign in service.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a migrated, disposable local database. Most
tests run inside the rollback-only ``db_session`` fixture; there the whole test is one transaction, so
``now()`` does not move and times are shifted with explicit inserts or updates.

The two concurrency tests need real connections, so they commit. They delete their nonces and signup
tickets afterwards and disable the ``ZZ_TEST_`` user they create; that user and its Google identity
stay behind (users cannot be deleted, and the identity of a user without a password cannot be either).
"""

import asyncio
import secrets
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import hash_token
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import decode_access_token
from app.db import get_engine, get_sessionmaker
from app.integrations.google import GoogleIdentity
from app.models.enums import UserRole
from app.models.user import User
from app.services import google_auth_service

pytestmark = pytest.mark.db

CLIENT_ID = "123456789012-dbtest.apps.googleusercontent.com"


class StaticVerifier:
    """Stands in for GoogleVerifier: every credential verifies to the given identity."""

    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity

    async def verify(self, credential: str) -> GoogleIdentity:
        del credential
        return self.identity


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


@pytest.fixture(autouse=True)
def google_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    return settings


def random_email(prefix: str = "zz-test-google") -> str:
    return f"{prefix}-{secrets.token_hex(4)}@example.com"


def random_subject() -> str:
    return "9" + "".join(secrets.choice("0123456789") for _ in range(20))


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
    with pytest.raises(DBAPIError) as exc_info:
        async with session.begin_nested():
            await session.execute(text(sql), params or {})
    return str(exc_info.value)


async def insert_identity(session: AsyncSession, user_id: UUID, subject: str, email: str) -> UUID:
    result = await session.execute(
        text(
            "insert into public.user_identities (user_id, provider, subject, email, email_verified, name)"
            " values (:user_id, 'google', :subject, :email, true, 'ZZ_TEST_ identity') returning id"
        ),
        {"user_id": user_id, "subject": subject, "email": email},
    )
    return result.scalar_one()


async def sign_in(session: AsyncSession, identity_values: dict):
    nonce_out = await google_auth_service.issue_nonce(session)
    identity = GoogleIdentity(nonce=nonce_out.nonce, **identity_values)
    verifier = StaticVerifier(identity)
    return await google_auth_service.sign_in(session, verifier, "credential-not-checked", nonce_out.nonce)


# users.password_set ------------------------------------------------------------------------------


async def test_password_set_defaults_to_true_and_never_goes_back(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await db_session.refresh(user)
    assert user.password_set is True
    message = await expect_database_error(
        db_session, "update public.users set password_set = false where id = :id", {"id": user.id}
    )
    assert "password_set cannot change from true to false" in message
    google_only = await make_user(db_session, UserRole.BUYER, password_set=False)
    await db_session.execute(
        text("update public.users set password_set = true where id = :id"), {"id": google_only.id}
    )


# user_identities ---------------------------------------------------------------------------------


async def test_identity_constraints(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    other = await make_user(db_session, UserRole.BUYER)
    subject = random_subject()
    await insert_identity(db_session, user.id, subject, user.email)
    insert = (
        "insert into public.user_identities (user_id, provider, subject, email, email_verified)"
        " values (:user_id, :provider, :subject, :email, :verified)"
    )
    mine = {"provider": "google", "verified": True, "user_id": user.id, "email": user.email}
    theirs = {"provider": "google", "verified": True, "user_id": other.id, "email": other.email}
    # A second Google identity for the same user.
    await expect_database_error(db_session, insert, {**mine, "subject": random_subject()})
    # The same subject for another user.
    await expect_database_error(db_session, insert, {**theirs, "subject": subject})
    # Upper case email, email_verified false, another provider.
    await expect_database_error(
        db_session, insert, {**theirs, "subject": random_subject(), "email": "Upper@Example.com"}
    )
    await expect_database_error(db_session, insert, {**theirs, "subject": random_subject(), "verified": False})
    await expect_database_error(db_session, insert, {**theirs, "subject": random_subject(), "provider": "github"})
    # The valid identity for the other user is accepted.
    await db_session.execute(text(insert), {**theirs, "subject": random_subject()})


async def test_identity_columns_are_immutable_and_delete_needs_a_password(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    other = await make_user(db_session, UserRole.BUYER)
    identity_id = await insert_identity(db_session, user.id, random_subject(), user.email)
    update = "update public.user_identities set {} where id = :id"
    for change in (
        "user_id = :other",
        "subject = 'changed'",
        "email = 'changed@example.com'",
        "provider = 'google', created_at = now() - interval '1 day'",
    ):
        await expect_database_error(db_session, update.format(change), {"id": identity_id, "other": other.id})
    await db_session.execute(text(update.format("name = 'Renamed', last_login_at = now()")), {"id": identity_id})
    delete = "delete from public.user_identities where id = :id"
    # With a password the identity may be removed.
    await db_session.execute(text(delete), {"id": identity_id})

    google_only = await make_user(db_session, UserRole.BUYER, password_set=False)
    only_id = await insert_identity(db_session, google_only.id, random_subject(), google_only.email)
    message = await expect_database_error(db_session, delete, {"id": only_id})
    assert "cannot be removed" in message


# oauth_nonces and google_signups -----------------------------------------------------------------


async def test_a_used_nonce_cannot_be_unused(db_session: AsyncSession) -> None:
    nonce_id = (
        await db_session.execute(
            text(
                "insert into public.oauth_nonces (nonce_hash, expires_at)"
                " values (:hash, now() + interval '5 minutes') returning id"
            ),
            {"hash": secrets.token_hex(32)},
        )
    ).scalar_one()
    update = "update public.oauth_nonces set {} where id = :id"
    for change in ("nonce_hash = 'changed'", "expires_at = now() + interval '1 day'"):
        await expect_database_error(db_session, update.format(change), {"id": nonce_id})
    await db_session.execute(text(update.format("used_at = now()")), {"id": nonce_id})
    await expect_database_error(db_session, update.format("used_at = null"), {"id": nonce_id})


async def test_one_open_ticket_per_subject(db_session: AsyncSession) -> None:
    subject = random_subject()
    insert = (
        "insert into public.google_signups (token_hash, google_sub, email, name, expires_at)"
        " values (:hash, :subject, :email, 'ZZ_TEST_', now() + interval '10 minutes') returning id"
    )
    values = {"subject": subject, "email": random_email()}
    first = (await db_session.execute(text(insert), {**values, "hash": secrets.token_hex(32)})).scalar_one()
    await expect_database_error(db_session, insert, {**values, "hash": secrets.token_hex(32)})
    update = "update public.google_signups set {} where id = :id"
    for change in ("email = 'other@example.com'", "google_sub = 'other'", "name = 'Other'"):
        await expect_database_error(db_session, update.format(change), {"id": first})
    await db_session.execute(text(update.format("used_at = now()")), {"id": first})
    await expect_database_error(db_session, update.format("used_at = null"), {"id": first})
    await db_session.execute(text(insert), {**values, "hash": secrets.token_hex(32)})  # allowed after use
    await expect_database_error(
        db_session,
        insert.replace(":email", "'Upper@Example.com'"),
        {"subject": random_subject(), "hash": secrets.token_hex(32)},
    )


# Security and jobs -------------------------------------------------------------------------------


async def test_row_level_security_privileges_and_cleanup_job(db_session: AsyncSession) -> None:
    for table in ("user_identities", "oauth_nonces", "google_signups"):
        enabled = await db_session.scalar(
            text("select relrowsecurity from pg_class where oid = cast(:name as regclass)"), {"name": f"public.{table}"}
        )
        assert enabled is True, table
        roles = ["anon", "authenticated"]
        if await db_session.scalar(text("select exists(select 1 from pg_roles where rolname = 'pipeline_writer')")):
            roles.append("pipeline_writer")
        for role in roles:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                allowed = await db_session.scalar(
                    text(
                        "select has_table_privilege(cast(:role as name), cast(:name as text), cast(:privilege as text))"
                    ),
                    {"role": role, "name": f"public.{table}", "privilege": privilege},
                )
                assert allowed is False, (role, table, privilege)
    jobs = await db_session.scalar(
        text("select count(*) from cron.job where jobname = 'cleanup-google-auth' and schedule = '30 2 * * *'")
    )
    assert jobs == 1


async def test_the_cleanup_removes_only_old_nonces_and_tickets(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    identity_id = await insert_identity(db_session, user.id, random_subject(), user.email)
    old_nonce = (
        await db_session.execute(
            text(
                "insert into public.oauth_nonces (nonce_hash, created_at, expires_at)"
                " values (:hash, now() - interval '3 days', now() - interval '3 days') returning id"
            ),
            {"hash": secrets.token_hex(32)},
        )
    ).scalar_one()
    fresh_nonce = (
        await db_session.execute(
            text("insert into public.oauth_nonces (nonce_hash, expires_at) values (:hash, now()) returning id"),
            {"hash": secrets.token_hex(32)},
        )
    ).scalar_one()
    old_ticket = (
        await db_session.execute(
            text(
                "insert into public.google_signups (token_hash, google_sub, email, created_at, expires_at, used_at)"
                " values (:hash, :subject, :email, now() - interval '3 days', now(), now()) returning id"
            ),
            {"hash": secrets.token_hex(32), "subject": random_subject(), "email": random_email()},
        )
    ).scalar_one()
    await db_session.scalar(text("select public.cleanup_google_auth()"))
    remaining_nonces = set((await db_session.execute(text("select id from public.oauth_nonces"))).scalars().all())
    assert fresh_nonce in remaining_nonces and old_nonce not in remaining_nonces
    tickets = await db_session.scalar(
        text("select count(*) from public.google_signups where id = :id"), {"id": old_ticket}
    )
    identities = await db_session.scalar(
        text("select count(*) from public.user_identities where id = :id"), {"id": identity_id}
    )
    assert (tickets, identities) == (0, 1)  # user_identities is never cleaned up


# Service flows -----------------------------------------------------------------------------------


async def test_a_new_google_user_end_to_end(db_session: AsyncSession) -> None:
    email, subject = random_email(), random_subject()
    started = await sign_in(db_session, {"subject": subject, "email": email, "name": "ZZ_TEST_ Google"})
    assert started.status == "role_required" and started.email == email
    assert await db_session.scalar(select(func.count()).select_from(User).where(User.email == email)) == 0
    ticket = (
        await db_session.execute(text("select * from public.google_signups where google_sub = :s"), {"s": subject})
    ).mappings().one()
    assert ticket["token_hash"] == hash_token(started.signup_token)

    result = await google_auth_service.complete(db_session, started.signup_token, "buyer")
    user = await db_session.scalar(
        select(User).where(User.id == UUID(decode_access_token(result.access_token)["sub"])).execution_options(
            populate_existing=True
        )
    )
    assert (user.email, user.role, user.password_set, user.must_change_password) == (
        email,
        UserRole.BUYER,
        False,
        False,
    )
    assert user.email_verified_at is not None and user.password_changed_at is not None
    assert await google_auth_service.is_google_linked(db_session, user.id)
    with pytest.raises(AppError) as exc_info:
        await google_auth_service.complete(db_session, started.signup_token, "buyer")
    assert exc_info.value.code == "invalid_or_expired_ticket"

    again = await sign_in(db_session, {"subject": subject, "email": email, "name": "ZZ_TEST_ Google"})
    assert again.status == "signed_in" and again.role == "buyer"


async def test_linking_admins_conflicts_and_replayed_nonces(db_session: AsyncSession) -> None:
    farmer = await make_user(db_session)
    linked = await sign_in(db_session, {"subject": random_subject(), "email": farmer.email, "name": "ZZ"})
    assert linked.status == "signed_in" and linked.role == "farmer"

    with pytest.raises(AppError) as exc_info:
        await sign_in(db_session, {"subject": random_subject(), "email": farmer.email, "name": "ZZ"})
    assert exc_info.value.code == "google_conflict"

    admin = await make_user(db_session, UserRole.ADMIN)
    with pytest.raises(AppError) as exc_info:
        await sign_in(db_session, {"subject": random_subject(), "email": admin.email, "name": "ZZ"})
    assert exc_info.value.code == "google_not_allowed"
    assert not await google_auth_service.is_google_linked(db_session, admin.id)

    nonce_out = await google_auth_service.issue_nonce(db_session)
    identity = GoogleIdentity(subject=random_subject(), email=random_email(), name="ZZ", nonce=nonce_out.nonce)
    await google_auth_service.sign_in(db_session, StaticVerifier(identity), "credential", nonce_out.nonce)
    with pytest.raises(AppError) as exc_info:
        await google_auth_service.sign_in(db_session, StaticVerifier(identity), "credential", nonce_out.nonce)
    assert exc_info.value.code == "google_sign_in_failed"


async def test_an_email_taken_before_complete_keeps_the_ticket(db_session: AsyncSession) -> None:
    email, subject = random_email(), random_subject()
    started = await sign_in(db_session, {"subject": subject, "email": email, "name": "ZZ_TEST_"})
    await make_user(db_session, UserRole.FARMER, email=email)
    with pytest.raises(AppError) as exc_info:
        await google_auth_service.complete(db_session, started.signup_token, "buyer")
    assert exc_info.value.code == "google_retry"
    used_at = await db_session.scalar(
        text("select used_at from public.google_signups where token_hash = :h"), {"h": hash_token(started.signup_token)}
    )
    assert used_at is None


# Concurrency (commits; see the module docstring) -------------------------------------------------


async def test_two_concurrent_completes_create_exactly_one_user() -> None:
    factory = get_sessionmaker()
    email, subject = random_email("zz-test-google-concurrent"), random_subject()
    async with factory() as session:
        started = await sign_in(session, {"subject": subject, "email": email, "name": "ZZ_TEST_ concurrent"})

    async def attempt() -> str:
        async with factory() as session:
            try:
                await google_auth_service.complete(session, started.signup_token, "farmer")
            except AppError as exc:
                return exc.code
            return "created"

    try:
        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == ["created", "invalid_or_expired_ticket"]
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(User).where(User.email == email))
            assert count == 1
    finally:
        async with factory() as session:
            await session.execute(text("delete from public.google_signups where google_sub = :s"), {"s": subject})
            await session.execute(
                text("update public.users set is_disabled = true, disabled_at = now() where email = :e"), {"e": email}
            )
            await session.commit()


async def test_two_concurrent_sign_ins_of_a_new_account_leave_one_open_ticket() -> None:
    factory = get_sessionmaker()
    email, subject = random_email("zz-test-google-parallel"), random_subject()
    identity = {"subject": subject, "email": email, "name": "ZZ_TEST_ parallel"}

    async def attempt():
        async with factory() as session:
            return await sign_in(session, identity)

    try:
        results = await asyncio.gather(attempt(), attempt())
        assert [result.status for result in results] == ["role_required", "role_required"]
        async with factory() as session:
            open_tickets = await session.scalar(
                text("select count(*) from public.google_signups where google_sub = :s and used_at is null"),
                {"s": subject},
            )
            assert open_tickets == 1
            working = 0
            for result in results:
                try:
                    await google_auth_service.complete(session, result.signup_token, "farmer")
                    working += 1
                except AppError as exc:
                    assert exc.code == "invalid_or_expired_ticket"
            assert working == 1
    finally:
        async with factory() as session:
            await session.execute(text("delete from public.google_signups where google_sub = :s"), {"s": subject})
            await session.execute(
                text("update public.users set is_disabled = true, disabled_at = now() where email = :e"), {"e": email}
            )
            await session.commit()
