"""Database tests for invites and password resets against migration 20261016000100.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a migrated, disposable local database. All
tests except the concurrency test run inside the rollback-only ``db_session`` fixture. Inside it the
whole test is one transaction, so ``now()`` does not move: times are shifted with explicit updates.

The concurrency test needs two real connections, so it commits: it leaves two disabled ``ZZ_TEST_``
users behind (users cannot be deleted) and deletes its invite rows.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from invite_fakes import FakeMailer, configure_codes
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import hash_code
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import verify_password
from app.db import get_engine, get_sessionmaker
from app.integrations import email as email_integration
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.invite import AcceptInviteRequest, CreateInviteRequest, PasswordResetConfirmRequest
from app.services import auth_service, invite_service, password_reset_service

pytestmark = pytest.mark.db

PASSWORD = "Mavuno-Ledger-2026!"


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    return configure_codes(monkeypatch, get_settings())


@pytest.fixture
def mailer(monkeypatch):
    fake = FakeMailer()
    monkeypatch.setattr(email_integration, "send_email", fake.send_email)
    return fake


def random_phone() -> str:
    return f"2547{secrets.randbelow(10**8):08d}"


def random_email(prefix: str = "invitee") -> str:
    return f"{prefix}-{secrets.token_hex(4)}@example.com"


async def make_user(session: AsyncSession, role: UserRole = UserRole.ADMIN, **values) -> User:
    values.setdefault("name", f"ZZ_TEST_ {role.value}")
    values.setdefault("phone_number", random_phone())
    values.setdefault("password_hash", "not-a-real-hash")
    user = User(role=role, **values)
    session.add(user)
    await session.commit()
    return user


async def create_invite(session: AsyncSession, inviter: User, mailer: FakeMailer, **body):
    request = CreateInviteRequest(
        name=body.get("name", "Grace Admin"),
        phone_number=body.get("phone_number", random_phone()),
        email=body.get("email", random_email()),
    )
    out = await invite_service.create_invite(session, inviter, request)
    return out, request, mailer.code()


async def invite_row(session: AsyncSession, invite_id: UUID):
    result = await session.execute(text("select * from public.admin_invites where id = :id"), {"id": invite_id})
    return result.mappings().one()


def accept(email: str, code: str, password: str = PASSWORD) -> AcceptInviteRequest:
    return AcceptInviteRequest(email=email, code=code, password=password)


async def expect_generic_failure(coroutine) -> None:
    with pytest.raises(AppError) as exc_info:
        await coroutine
    assert (exc_info.value.status_code, exc_info.value.code) == (400, "invalid_or_expired_code")


# Invites -------------------------------------------------------------------------------------


async def test_full_invite_flow_creates_an_admin_who_logs_in(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, request, code = await create_invite(db_session, inviter, mailer)

    stored = await invite_row(db_session, out.id)
    assert stored["code_hash"] == hash_code("invite", out.id, code) and code not in stored["code_hash"]
    assert (stored["send_count"], stored["attempts"], stored["created_by"]) == (1, 0, inviter.id)

    user = await invite_service.accept_invite(db_session, accept(request.email, code.lower()))
    created = await db_session.scalar(
        select(User).where(User.id == user["id"]).execution_options(populate_existing=True)
    )
    assert (created.role, created.is_super_admin, created.must_change_password) == (UserRole.ADMIN, False, False)
    assert created.created_by == inviter.id and created.password_changed_at is not None
    assert verify_password(created.password_hash, PASSWORD)
    assert (await invite_row(db_session, out.id))["used_at"] is not None

    auth_service.get_login_throttle().reset()
    login = await auth_service.login(db_session, request.email, PASSWORD)
    assert login["must_change_password"] is False
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, code)))


async def test_every_failure_type_is_the_same_400(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, request, code = await create_invite(db_session, inviter, mailer)

    await expect_generic_failure(invite_service.accept_invite(db_session, accept(random_email(), code)))
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, "ZZZZ-ZZZZ")))

    await db_session.execute(
        text("update public.admin_invites set code_expires_at = now() - interval '1 minute' where id = :id"),
        {"id": out.id},
    )
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, code)))

    revoked, revoked_request, revoked_code = await create_invite(db_session, inviter, mailer)
    await invite_service.revoke_invite(db_session, inviter, revoked.id)
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(revoked_request.email, revoked_code)))

    # An invite that expired (expires_at is immutable, so it is inserted already expired).
    email = random_email("expired")
    invite_id = UUID(int=secrets.randbits(128))
    expired_code = "ABCDEFGH"
    await db_session.execute(
        text(
            "insert into public.admin_invites"
            " (id, email, name, phone_number, code_hash, code_expires_at, created_by, expires_at)"
            " values (:id, :email, 'Expired', :phone, :hash, now() + interval '10 minutes', :by,"
            "         now() - interval '1 minute')"
        ),
        {
            "id": invite_id,
            "email": email,
            "phone": random_phone(),
            "hash": hash_code("invite", invite_id, expired_code),
            "by": inviter.id,
        },
    )
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(email, expired_code)))


async def test_attempts_are_committed_survive_resends_and_lock_at_ten(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, request, code = await create_invite(db_session, inviter, mailer)
    for _ in range(4):
        await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, "ZZZZ-ZZZZ")))
    assert (await invite_row(db_session, out.id))["attempts"] == 4

    await db_session.execute(
        text("update public.admin_invites set last_sent_at = now() - interval '5 minutes' where id = :id"),
        {"id": out.id},
    )
    await db_session.commit()
    await invite_service.resend_invite(db_session, inviter, out.id)
    new_code = mailer.code()
    stored = await invite_row(db_session, out.id)
    assert (stored["attempts"], stored["send_count"]) == (4, 2)
    if new_code != code:
        await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, code)))

    while (await invite_row(db_session, out.id))["attempts"] < 10:
        await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, "ZZZZ-ZZZZ")))
    assert (await invite_row(db_session, out.id))["locked_at"] is not None
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, new_code)))
    assert (await invite_row(db_session, out.id))["attempts"] == 10


async def test_resend_cooldown_and_send_cap(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, _, _ = await create_invite(db_session, inviter, mailer)
    with pytest.raises(AppError) as exc_info:
        await invite_service.resend_invite(db_session, inviter, out.id)
    assert exc_info.value.code == "resend_cooldown"
    for expected_count in (2, 3, 4, 5):
        await db_session.execute(
            text("update public.admin_invites set last_sent_at = now() - interval '5 minutes' where id = :id"),
            {"id": out.id},
        )
        await invite_service.resend_invite(db_session, inviter, out.id)
        assert (await invite_row(db_session, out.id))["send_count"] == expected_count
    await db_session.execute(
        text("update public.admin_invites set last_sent_at = now() - interval '5 minutes' where id = :id"),
        {"id": out.id},
    )
    with pytest.raises(AppError) as exc_info:
        await invite_service.resend_invite(db_session, inviter, out.id)
    assert exc_info.value.code == "send_limit_reached"


async def test_a_new_invite_replaces_the_pending_one(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    email = random_email()
    first, _, first_code = await create_invite(db_session, inviter, mailer, email=email)
    second, _, second_code = await create_invite(db_session, inviter, mailer, email=email)
    assert (await invite_row(db_session, first.id))["revoked_at"] is not None
    assert (await invite_row(db_session, second.id))["revoked_at"] is None
    if first_code != second_code:
        await expect_generic_failure(invite_service.accept_invite(db_session, accept(email, first_code)))
    await invite_service.accept_invite(db_session, accept(email, second_code))


async def test_an_existing_user_blocks_the_invite(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    farmer = await make_user(db_session, UserRole.FARMER, email=random_email("farmer"))
    with pytest.raises(AppError) as exc_info:
        await create_invite(db_session, inviter, mailer, email=farmer.email.upper())
    assert exc_info.value.code == "user_exists"


async def test_a_unique_violation_leaves_the_invite_unused(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, request, code = await create_invite(db_session, inviter, mailer)
    await make_user(db_session, UserRole.BUYER, phone_number=request.phone_number)
    await expect_generic_failure(invite_service.accept_invite(db_session, accept(request.email, code)))
    assert (await invite_row(db_session, out.id))["used_at"] is None


async def test_revoke_is_idempotent_and_refuses_used_invites(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, request, code = await create_invite(db_session, inviter, mailer)
    await invite_service.revoke_invite(db_session, inviter, out.id)
    await invite_service.revoke_invite(db_session, inviter, out.id)
    used, used_request, used_code = await create_invite(db_session, inviter, mailer)
    await invite_service.accept_invite(db_session, accept(used_request.email, used_code))
    with pytest.raises(AppError) as exc_info:
        await invite_service.revoke_invite(db_session, inviter, used.id)
    assert exc_info.value.code == "invite_not_pending"


# Constraints, triggers and row level security ------------------------------------------------


async def expect_database_error(session: AsyncSession, sql: str, params: dict) -> None:
    """Run ``sql`` in a savepoint that is rolled back when the database rejects it."""
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(text(sql), params)


async def test_constraints_and_triggers(db_session: AsyncSession, mailer) -> None:
    inviter = await make_user(db_session)
    out, request, _ = await create_invite(db_session, inviter, mailer)
    insert = (
        "insert into public.admin_invites"
        " (email, name, phone_number, code_hash, code_expires_at, created_by, expires_at)"
        " values (:email, 'Second', :phone, 'hash', now(), :by, now() + interval '1 day')"
    )
    await expect_database_error(db_session, insert, {"email": request.email, "phone": random_phone(), "by": inviter.id})
    await expect_database_error(
        db_session, insert, {"email": random_email(), "phone": request.phone_number, "by": inviter.id}
    )
    await expect_database_error(
        db_session, insert, {"email": "Upper@Example.com", "phone": random_phone(), "by": inviter.id}
    )
    update = "update public.admin_invites set {} where id = :id"
    await expect_database_error(db_session, update.format("send_count = 6"), {"id": out.id})
    await db_session.execute(text(update.format("attempts = 3")), {"id": out.id})
    await expect_database_error(db_session, update.format("attempts = 2"), {"id": out.id})
    for change in ("email = 'other@example.com'", "name = 'Other'", "expires_at = now()", "created_by = null"):
        await expect_database_error(db_session, update.format(change), {"id": out.id})
    await db_session.execute(text(update.format("used_at = now()")), {"id": out.id})
    await expect_database_error(db_session, update.format("used_at = null"), {"id": out.id})
    await expect_database_error(db_session, update.format("revoked_at = now()"), {"id": out.id})


async def test_one_active_reset_code_per_user(db_session: AsyncSession) -> None:
    user = await make_user(db_session, UserRole.FARMER)
    insert = (
        "insert into public.password_reset_codes (user_id, code_hash, code_expires_at)"
        " values (:user_id, 'hash', now() + interval '15 minutes')"
    )
    await db_session.execute(text(insert), {"user_id": user.id})
    await expect_database_error(db_session, insert, {"user_id": user.id})
    await db_session.execute(
        text("update public.password_reset_codes set used_at = now() where user_id = :user_id"), {"user_id": user.id}
    )
    await db_session.execute(text(insert), {"user_id": user.id})


async def test_row_level_security_and_no_anon_privileges(db_session: AsyncSession) -> None:
    for table in ("admin_invites", "password_reset_codes"):
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
    column = await db_session.scalar(
        text(
            "select data_type from information_schema.columns"
            " where table_schema = 'public' and table_name = 'users' and column_name = 'password_changed_at'"
        )
    )
    assert column == "timestamp with time zone"


# Password reset ------------------------------------------------------------------------------


async def request_code(session: AsyncSession, email: str, mailer: FakeMailer) -> str | None:
    tasks = BackgroundTasks()
    sent_before = len(mailer.sent)
    await password_reset_service.request_reset(session, email, tasks)
    await tasks()
    return mailer.code() if len(mailer.sent) > sent_before else None


async def test_password_reset_end_to_end(db_session: AsyncSession, mailer) -> None:
    email = random_email("farmer")
    user = await make_user(db_session, UserRole.FARMER, email=email, must_change_password=True)
    code = await request_code(db_session, email, mailer)
    assert code is not None
    new_password = "Shamba-Harvest-77"
    await password_reset_service.confirm_reset(
        db_session, PasswordResetConfirmRequest(email=email, code=code, new_password=new_password)
    )
    stored = await db_session.scalar(select(User).where(User.id == user.id).execution_options(populate_existing=True))
    assert verify_password(stored.password_hash, new_password)
    assert stored.must_change_password is False and stored.password_changed_at is not None
    auth_service.get_login_throttle().reset()
    assert (await auth_service.login(db_session, email, new_password))["role"] == "farmer"
    # Single use.
    await expect_generic_failure(
        password_reset_service.confirm_reset(
            db_session, PasswordResetConfirmRequest(email=email, code=code, new_password="Another-pass-88")
        )
    )


async def test_password_reset_limits(db_session: AsyncSession, mailer) -> None:
    email = random_email("buyer")
    user = await make_user(db_session, UserRole.BUYER, email=email)
    first = await request_code(db_session, email, mailer)
    assert first is not None
    assert await request_code(db_session, email, mailer) is None  # cooldown
    for _ in range(2):
        await db_session.execute(
            text(
                "update public.password_reset_codes set created_at = created_at - interval '2 minutes'"
                " where user_id = :user_id"
            ),
            {"user_id": user.id},
        )
        assert await request_code(db_session, email, mailer) is not None
    await db_session.execute(
        text(
            "update public.password_reset_codes set created_at = created_at - interval '2 minutes'"
            " where user_id = :user_id"
        ),
        {"user_id": user.id},
    )
    assert await request_code(db_session, email, mailer) is None  # 3 per hour
    active = await db_session.scalar(
        text("select count(*) from public.password_reset_codes where user_id = :user_id and used_at is null"),
        {"user_id": user.id},
    )
    assert active == 1
    latest = mailer.code()
    if latest != first:
        await expect_generic_failure(
            password_reset_service.confirm_reset(
                db_session, PasswordResetConfirmRequest(email=email, code=first, new_password="Superseded-1!")
            )
        )
    for _ in range(5):
        await expect_generic_failure(
            password_reset_service.confirm_reset(
                db_session, PasswordResetConfirmRequest(email=email, code="ZZZZ-ZZZZ", new_password="Wrong-guess-1!")
            )
        )
    locked = await db_session.scalar(
        text("select locked_at from public.password_reset_codes where user_id = :user_id and used_at is null"),
        {"user_id": user.id},
    )
    assert locked is not None
    await expect_generic_failure(
        password_reset_service.confirm_reset(
            db_session, PasswordResetConfirmRequest(email=email, code=latest, new_password="Correct-but-late-1!")
        )
    )


async def test_password_reset_expiry_and_disabled_user(db_session: AsyncSession, mailer) -> None:
    email = random_email("farmer")
    user = await make_user(db_session, UserRole.FARMER, email=email)
    code = await request_code(db_session, email, mailer)
    await db_session.execute(
        text(
            "update public.password_reset_codes set code_expires_at = now() - interval '1 minute' where user_id = :id"
        ),
        {"id": user.id},
    )
    await expect_generic_failure(
        password_reset_service.confirm_reset(
            db_session, PasswordResetConfirmRequest(email=email, code=code, new_password="Shamba-Harvest-77")
        )
    )

    # A disabled account gets no code, and a code issued before disabling stops working.
    other_email = random_email("disabled")
    other = await make_user(db_session, UserRole.FARMER, email=other_email)
    issued = await request_code(db_session, other_email, mailer)
    assert issued is not None
    await db_session.execute(
        text("update public.users set is_disabled = true, disabled_at = now() where id = :id"), {"id": other.id}
    )
    await db_session.execute(
        text(
            "update public.password_reset_codes set created_at = created_at - interval '2 minutes' where user_id = :id"
        ),
        {"id": other.id},
    )
    assert await request_code(db_session, other_email, mailer) is None
    await expect_generic_failure(
        password_reset_service.confirm_reset(
            db_session, PasswordResetConfirmRequest(email=other_email, code=issued, new_password="Shamba-Harvest-77")
        )
    )


# Concurrency (commits; see the module docstring) ---------------------------------------------


async def test_two_concurrent_accepts_create_exactly_one_user(mailer) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        inviter = await make_user(session, name="ZZ_TEST_ inviter")
        out, request, code = await create_invite(session, inviter, mailer)

    async def attempt() -> str:
        async with factory() as session:
            try:
                await invite_service.accept_invite(session, accept(request.email, code))
            except AppError as exc:
                return exc.code
            return "created"

    try:
        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == ["created", "invalid_or_expired_code"]
        async with factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(User).where(User.phone_number == request.phone_number)
            )
            assert count == 1
    finally:
        async with factory() as session:
            await session.execute(text("delete from public.admin_invites where id = :id"), {"id": out.id})
            await session.execute(
                text(
                    "update public.users set is_disabled = true, disabled_at = now()"
                    " where (id = :inviter or phone_number = :phone) and not is_super_admin"
                ),
                {"inviter": inviter.id, "phone": request.phone_number},
            )
            await session.commit()
