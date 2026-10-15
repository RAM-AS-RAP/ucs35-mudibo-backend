"""Password reset by emailed code, for any enabled account that has an email address.

``request_reset`` always leads to the same 202 response; the email is sent in a background task so
the response time does not show whether the address exists. ``confirm_reset`` uses the same two steps
as invite acceptance: the attempt is counted in its own committed transaction (locking the code after
``RESET_MAX_ATTEMPTS`` wrong tries), then one transaction claims the code and sets the new password.
Every failure, a disabled account and an unknown email return the same 400 ``invalid_or_expired_code``.

Farmers and buyers who registered without an email address cannot use this flow (documented limitation).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import BackgroundTasks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import burn_dummy_check, ensure_codes_configured, generate_code, hash_code, verify_code
from app.core.config import get_settings
from app.core.password_policy import validate_new_password
from app.core.security import hash_password
from app.integrations.email_templates import reset_email
from app.schemas.invite import PasswordResetConfirmRequest
from app.services.invite_service import deliver_quietly, invalid_code

logger = logging.getLogger(__name__)


async def request_reset(session: AsyncSession, email: str, background_tasks: BackgroundTasks) -> None:
    """Issue a new code when allowed; otherwise do the same hashing work and nothing else."""
    ensure_codes_configured()
    settings = get_settings()
    code = generate_code()
    result = await session.execute(
        text("select id, email from public.users where lower(email) = :email and is_disabled = false   for update"),
        {"email": email},
    )
    user = result.mappings().first()
    allowed = False
    if user is not None:
        result = await session.execute(
            text(
                "select count(*) filter (where created_at > now() - interval '1 hour') as recent,"
                "       coalesce(bool_or(created_at > now() - cast(:cooldown as integer) * interval '1 second'),"
                "                false) as in_cooldown"
                "  from public.password_reset_codes"
                " where user_id = :user_id"
            ),
            {"user_id": user["id"], "cooldown": settings.invite_resend_cooldown_seconds},
        )
        recent = result.mappings().one()
        allowed = int(recent["recent"] or 0) < settings.reset_max_requests_per_hour and not recent["in_cooldown"]
    if user is None or not allowed:
        await session.commit()
        burn_dummy_check("reset", code)
        return

    # Supersede the active code (at most one, by the partial unique index), then store the new one.
    await session.execute(
        text("update public.password_reset_codes set used_at = now() where user_id = :user_id and used_at is null"),
        {"user_id": user["id"]},
    )
    code_id = uuid4()
    await session.execute(
        text(
            "insert into public.password_reset_codes (id, user_id, code_hash, code_expires_at)"
            " values (:id, :user_id, :code_hash, now() + make_interval(mins => cast(:code_minutes as integer)))"
        ),
        {
            "id": code_id,
            "user_id": user["id"],
            "code_hash": hash_code("reset", code_id, code),
            "code_minutes": settings.reset_code_ttl_minutes,
        },
    )
    await session.commit()
    logger.info("password_reset_requested user=%s", user["id"])
    background_tasks.add_task(deliver_quietly, reset_email(user["email"], code, settings.reset_code_ttl_minutes))


async def confirm_reset(session: AsyncSession, body: PasswordResetConfirmRequest) -> None:
    """Set a new password from a valid code; see the module docstring for the two steps."""
    ensure_codes_configured()
    settings = get_settings()
    # Rules that do not depend on the account (role, name, phone) are checked before the code, so the
    # answer reveals nothing about whether the email exists.
    validate_new_password(body.new_password, None, email=body.email)

    # Step A: count the attempt in its own committed transaction.
    result = await session.execute(
        text(
            "update public.password_reset_codes as c set attempts = c.attempts + 1"
            "  from public.users as u"
            " where u.id = c.user_id and lower(u.email) = :email and u.is_disabled = false"
            "   and c.used_at is null and c.locked_at is null and c.code_expires_at > now()"
            "   and c.attempts < :max_attempts"
            " returning c.id, c.user_id, c.code_hash, c.attempts, cast(u.role as text) as role, u.name,"
            "           u.phone_number, u.email"
        ),
        {"email": body.email, "max_attempts": settings.reset_max_attempts},
    )
    attempt = result.mappings().first()
    if attempt is None:
        await session.commit()
        burn_dummy_check("reset", body.code)
        raise invalid_code()
    if not verify_code("reset", attempt["id"], body.code, attempt["code_hash"]):
        if attempt["attempts"] >= settings.reset_max_attempts:
            await session.execute(
                text("update public.password_reset_codes set locked_at = now() where id = :id and locked_at is null"),
                {"id": attempt["id"]},
            )
            logger.info("password_reset_locked user=%s", attempt["user_id"])
        await session.commit()
        raise invalid_code()
    await session.commit()

    validate_new_password(
        body.new_password,
        attempt["role"],
        email=attempt["email"],
        name=attempt["name"],
        phone=attempt["phone_number"],
    )
    password_hash = hash_password(body.new_password)

    # Step B: claim the code and replace the password together.
    result = await session.execute(
        text(
            "update public.password_reset_codes set used_at = now()"
            " where id = :id and used_at is null and locked_at is null and code_expires_at > now()"
            " returning user_id"
        ),
        {"id": attempt["id"]},
    )
    if result.mappings().first() is None:
        await session.rollback()
        raise invalid_code()
    result = await session.execute(
        text(
            "update public.users"
            "   set password_hash = :password_hash, must_change_password = false, password_changed_at = now()"
            " where id = :user_id and is_disabled = false"
            " returning id"
        ),
        {"password_hash": password_hash, "user_id": attempt["user_id"]},
    )
    if result.mappings().first() is None:
        await session.rollback()
        raise invalid_code()
    await session.commit()
    logger.info("password_reset_completed user=%s", attempt["user_id"])
