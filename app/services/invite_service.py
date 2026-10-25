"""Administrator invitations by emailed code.

The super admin creates, resends, lists and revokes invites (name and email only); the invited person
accepts with their email, the code and a password they choose, which creates an ordinary admin and
signs them in (the code proves control of the email, so no sign in code follows). Nobody ever sees a
credential: the code is emailed, only its HMAC is stored, and it is never logged or returned.

Accepting uses two steps. Step A increments the attempt counter in its own committed transaction
(so failed guesses always count and lock the invite at ``INVITE_MAX_ATTEMPTS``). Step B claims the
invite (``used_at``) and inserts the user in one transaction; a unique violation rolls both back.
Every failure returns the same 400 ``invalid_or_expired_code``.

The ``admin_invites`` table is queried with SQL text (it has no ORM mapping); every time comes from
the database clock.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_result import AuthResult, signed_in_result
from app.core.codes import burn_dummy_check, ensure_codes_configured, generate_code, hash_code, verify_code
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.password_policy import validate_new_password
from app.core.security import hash_password
from app.integrations import email as email_integration
from app.integrations.email_templates import invite_email
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.common import Pagination
from app.schemas.invite import AcceptInviteRequest, CreateInviteRequest, InviteOut, InvitePage, InviteSentOut
from app.services.email_delivery import deliver, deliver_quietly

logger = logging.getLogger(__name__)

__all__ = [
    "accept_invite",
    "create_invite",
    "deliver",
    "deliver_quietly",
    "invalid_code",
    "list_invites",
    "request_invite_code",
    "resend_invite",
    "revoke_invite",
]

PENDING_CONDITION = "used_at is null and revoked_at is null and locked_at is null and expires_at > now()"
SENT_MESSAGE = "Invitation sent. The code in the email is valid for {minutes} minutes."
NOT_SENT_MESSAGE = "The invitation was saved but the email could not be sent. Use the resend route to send a new code."


def invalid_code() -> AppError:
    """The one response for every accept or reset failure, so no failure can be told apart."""
    return AppError(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_code", "The code is invalid or has expired.")


def invite_not_found() -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, "invite_not_found", "Invite not found.")


def invite_not_pending() -> AppError:
    return AppError(status.HTTP_409_CONFLICT, "invite_not_pending", "This invite is no longer pending.")


def _sent_out(row: Any, email_sent: bool) -> InviteSentOut:
    minutes = get_settings().invite_code_ttl_minutes
    return InviteSentOut(
        id=row["id"],
        name=row["name"],
        email_masked=email_integration.mask_email(row["email"]),
        expires_at=row["expires_at"],
        code_expires_at=row["code_expires_at"],
        email_sent=email_sent,
        message=SENT_MESSAGE.format(minutes=minutes) if email_sent else NOT_SENT_MESSAGE,
    )


async def _store_new_code(session: AsyncSession, invite_id: UUID, code: str) -> Any:
    """Replace the code (the previous one stops working); ``attempts`` is deliberately not reset."""
    result = await session.execute(
        text(
            "update public.admin_invites"
            "   set code_hash = :code_hash,"
            "       code_expires_at = now() + make_interval(mins => cast(:code_minutes as integer)),"
            "       last_sent_at = now(),"
            "       send_count = send_count + 1"
            " where id = :id"
            " returning id, name, email, expires_at, code_expires_at"
        ),
        {
            "id": invite_id,
            "code_hash": hash_code("invite", invite_id, code),
            "code_minutes": get_settings().invite_code_ttl_minutes,
        },
    )
    return result.mappings().one()


# Super admin ----------------------------------------------------------------------------------


async def create_invite(session: AsyncSession, actor: User, body: CreateInviteRequest) -> InviteSentOut:
    """Create (or replace) a pending invite, commit, then email the code."""
    ensure_codes_configured()
    settings = get_settings()
    params = {"email": body.email}
    # Serialises invite creation so the pending cap cannot be exceeded by concurrent requests.
    await session.execute(text("select pg_advisory_xact_lock(hashtext('public.admin_invites'))"))
    user_exists = await session.scalar(
        text("select exists(select 1 from public.users where lower(email) = :email)"),
        params,
    )
    if user_exists:
        await session.rollback()
        raise AppError(status.HTTP_409_CONFLICT, "user_exists", "An account with this email already exists.")
    # The newest invite replaces any older one for the same email (also expired or locked ones, which
    # still hold the one-pending-invite unique index).
    await session.execute(
        text(
            "update public.admin_invites set revoked_at = now()"
            " where email = :email and used_at is null and revoked_at is null"
        ),
        params,
    )
    pending = await session.scalar(text("select count(*) from public.admin_invites where " + PENDING_CONDITION))
    if int(pending or 0) >= settings.invite_max_pending:
        await session.rollback()
        raise AppError(
            status.HTTP_409_CONFLICT,
            "too_many_pending_invites",
            "Too many invites are pending. Revoke one or wait until one is accepted or expires.",
        )
    invite_id = uuid4()
    code = generate_code()
    try:
        result = await session.execute(
            text(
                "insert into public.admin_invites"
                " (id, email, name, code_hash, code_expires_at, created_by, expires_at)"
                " values (:id, :email, :name, :code_hash,"
                "         now() + make_interval(mins => cast(:code_minutes as integer)),"
                "         :created_by,"
                "         now() + make_interval(days => cast(:days as integer)))"
                " returning id, name, email, expires_at, code_expires_at"
            ),
            {
                **params,
                "id": invite_id,
                "name": body.name,
                "code_hash": hash_code("invite", invite_id, code),
                "code_minutes": settings.invite_code_ttl_minutes,
                "created_by": actor.id,
                "days": settings.invite_ttl_days,
            },
        )
        row = result.mappings().one()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(
            status.HTTP_409_CONFLICT, "invite_conflict", "Another invite for these details was just created."
        ) from None
    logger.info("admin_invite_created actor=%s invite=%s", actor.id, invite_id)
    email_sent = await deliver(invite_email(row["email"], actor.name, code, settings.invite_code_ttl_minutes))
    return _sent_out(row, email_sent)


async def resend_invite(session: AsyncSession, actor: User, invite_id: UUID) -> InviteSentOut:
    """Send a new code for a pending invite, respecting the cooldown and the send cap."""
    ensure_codes_configured()
    settings = get_settings()
    result = await session.execute(
        text(
            "select i.id, i.used_at, i.revoked_at, i.locked_at, i.send_count, u.name as inviter_name,"
            "       i.expires_at <= now() as expired,"
            "       i.last_sent_at > now() - cast(:cooldown as integer) * interval '1 second' as in_cooldown"
            "  from public.admin_invites as i"
            "  left join public.users as u on u.id = i.created_by"
            " where i.id = :id"
            "   for update of i"
        ),
        {"id": invite_id, "cooldown": settings.invite_resend_cooldown_seconds},
    )
    current = result.mappings().first()
    if current is None:
        await session.rollback()
        raise invite_not_found()
    if current["used_at"] or current["revoked_at"] or current["locked_at"] or current["expired"]:
        await session.rollback()
        raise invite_not_pending()
    if current["in_cooldown"]:
        await session.rollback()
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS, "resend_cooldown", "Wait a minute before sending another code."
        )
    if current["send_count"] >= settings.invite_max_sends:
        await session.rollback()
        raise AppError(
            status.HTTP_409_CONFLICT,
            "send_limit_reached",
            "No more codes can be sent for this invite. Revoke it and create a new one.",
        )
    code = generate_code()
    row = await _store_new_code(session, invite_id, code)
    await session.commit()
    logger.info("admin_invite_resent actor=%s invite=%s", actor.id, invite_id)
    inviter = current["inviter_name"] or actor.name
    email_sent = await deliver(invite_email(row["email"], inviter, code, settings.invite_code_ttl_minutes))
    return _sent_out(row, email_sent)


_STATUS_SQL = (
    "case when i.used_at is not null then 'used'"
    "     when i.revoked_at is not null then 'revoked'"
    "     when i.locked_at is not null then 'locked'"
    "     when i.expires_at <= now() then 'expired'"
    "     else 'pending' end"
)
_LIST_CTE = (
    "with listed as ("
    " select i.id, i.name, i.email, i.created_at, i.expires_at, i.send_count, i.attempts,"
    "        i.used_at, i.revoked_at, u.name as created_by_name, " + _STATUS_SQL + " as status"
    "   from public.admin_invites as i"
    "   left join public.users as u on u.id = i.created_by"
    ")"
)
_STATUS_FILTER = " where (cast(:status as text) is null or status = cast(:status as text))"


async def list_invites(session: AsyncSession, pagination: Pagination, status_filter: str | None) -> InvitePage:
    """Invites newest first; never a code or a hash."""
    params: dict[str, Any] = {"status": status_filter}
    total = await session.scalar(text(_LIST_CTE + " select count(*) from listed" + _STATUS_FILTER), params)
    result = await session.execute(
        text(
            _LIST_CTE
            + " select id, name, email, status, created_by_name, created_at, expires_at, send_count, attempts,"
            " used_at, revoked_at from listed"
            + _STATUS_FILTER
            + " order by created_at desc, id desc limit :limit offset :offset"
        ),
        {**params, "limit": pagination.limit, "offset": pagination.offset},
    )
    items = [
        InviteOut(
            id=row["id"],
            name=row["name"],
            email_masked=email_integration.mask_email(row["email"]),
            status=row["status"],
            created_by_name=row["created_by_name"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            send_count=row["send_count"],
            attempts=row["attempts"],
            used_at=row["used_at"],
            revoked_at=row["revoked_at"],
        )
        for row in result.mappings().all()
    ]
    return InvitePage(items=items, total=int(total or 0), limit=pagination.limit, offset=pagination.offset)


async def revoke_invite(session: AsyncSession, actor: User, invite_id: UUID) -> None:
    """Revoke a pending invite; repeating it is a no-op, and a used invite cannot be revoked."""
    result = await session.execute(
        text("select used_at, revoked_at from public.admin_invites where id = :id for update"), {"id": invite_id}
    )
    current = result.mappings().first()
    if current is None:
        await session.rollback()
        raise invite_not_found()
    if current["used_at"] is not None:
        await session.rollback()
        raise invite_not_pending()
    if current["revoked_at"] is None:
        await session.execute(
            text("update public.admin_invites set revoked_at = now() where id = :id"), {"id": invite_id}
        )
        logger.info("admin_invite_revoked actor=%s invite=%s", actor.id, invite_id)
    await session.commit()


# Public ---------------------------------------------------------------------------------------


async def accept_invite(session: AsyncSession, body: AcceptInviteRequest) -> AuthResult:
    """Create an ordinary admin from a valid code and sign them in; see the module docstring."""
    ensure_codes_configured()
    settings = get_settings()
    # Only rules that reveal nothing about any invite are checked before the code.
    validate_new_password(body.password, UserRole.ADMIN, email=body.email)

    # Step A: count the attempt in its own committed transaction.
    result = await session.execute(
        text(
            "update public.admin_invites set attempts = attempts + 1"
            " where email = :email and used_at is null and revoked_at is null and locked_at is null"
            "   and expires_at > now() and code_expires_at > now() and attempts < :max_attempts"
            " returning id, name, code_hash, attempts"
        ),
        {"email": body.email, "max_attempts": settings.invite_max_attempts},
    )
    attempt = result.mappings().first()
    if attempt is None:
        await session.commit()
        burn_dummy_check("invite", body.code)
        raise invalid_code()
    if not verify_code("invite", attempt["id"], body.code, attempt["code_hash"]):
        if attempt["attempts"] >= settings.invite_max_attempts:
            await session.execute(
                text("update public.admin_invites set locked_at = now() where id = :id and locked_at is null"),
                {"id": attempt["id"]},
            )
            logger.info("admin_invite_locked invite=%s", attempt["id"])
        await session.commit()
        raise invalid_code()
    await session.commit()

    validate_new_password(body.password, UserRole.ADMIN, email=body.email, name=attempt["name"])
    password_hash = hash_password(body.password)

    # Step B: claim the invite and create the admin together.
    try:
        result = await session.execute(
            text(
                "update public.admin_invites set used_at = now()"
                " where id = :id and used_at is null and revoked_at is null and locked_at is null"
                "   and expires_at > now()"
                " returning name, email, created_by"
            ),
            {"id": attempt["id"]},
        )
        claimed = result.mappings().first()
        if claimed is None:
            await session.rollback()
            raise invalid_code()
        result = await session.execute(
            text(
                "insert into public.users"
                " (name, email, password_hash, role, is_super_admin, must_change_password,"
                "  created_by, password_changed_at, email_verified_at)"
                " values (:name, :email, :password_hash, 'admin', false, false, :created_by, now(), now())"
                " returning id, name, email, cast(role as text) as role, is_super_admin,"
                "           must_change_password, created_at"
            ),
            {
                "name": claimed["name"],
                "email": claimed["email"],
                "password_hash": password_hash,
                "created_by": claimed["created_by"],
            },
        )
        user = dict(result.mappings().one())
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise invalid_code() from None
    logger.info("admin_invite_accepted invite=%s user=%s", attempt["id"], user["id"])
    return signed_in_result(user)


async def request_invite_code(session: AsyncSession, email: str, background_tasks: BackgroundTasks) -> None:
    """Send a new code when a pending invite allows it; the caller always answers the same 202."""
    ensure_codes_configured()
    settings = get_settings()
    code = generate_code()
    result = await session.execute(
        text(
            "select i.id, u.name as inviter_name"
            "  from public.admin_invites as i"
            "  left join public.users as u on u.id = i.created_by"
            " where i.email = :email and i.used_at is null and i.revoked_at is null and i.locked_at is null"
            "   and i.expires_at > now() and i.send_count < :max_sends"
            "   and i.last_sent_at <= now() - cast(:cooldown as integer) * interval '1 second'"
            "   for update of i"
        ),
        {"email": email, "max_sends": settings.invite_max_sends, "cooldown": settings.invite_resend_cooldown_seconds},
    )
    current = result.mappings().first()
    if current is None:
        await session.commit()
        burn_dummy_check("invite", code)
        return
    row = await _store_new_code(session, current["id"], code)
    await session.commit()
    message = invite_email(
        row["email"], current["inviter_name"] or "the AgriSence administrator", code, settings.invite_code_ttl_minutes
    )
    background_tasks.add_task(deliver_quietly, message)
