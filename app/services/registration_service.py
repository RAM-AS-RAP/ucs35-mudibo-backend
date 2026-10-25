"""Farmer and buyer registration confirmed by an emailed code.

``register`` never reveals whether an address has an account: it always hashes the password, always
answers 202 with the same body shape and sends any email in a background task.

- An address that already has an account: nothing is stored. The "account exists" notice is emailed
  (at most once per ``REG_EXISTS_NOTICE_MINUTES`` per address, tracked in this process only) and the
  response carries a fresh random token that is stored nowhere, so it can never verify.
- Otherwise a ``pending_registrations`` row holds the name, role, password hash, a token hash and a
  code hash. Submitting again rotates the token (the previous token stops working) and keeps the code
  while it is younger than ``REG_RESEND_COOLDOWN_SECONDS``; after that a new code is sent, up to
  ``REG_MAX_SENDS``. A locked row, or one for the other role (role is immutable in the database), is
  closed and replaced by a new row.

``verify`` uses the same two steps as invite acceptance: the attempt is counted in its own committed
transaction before the comparison (locking the row at ``REG_MAX_ATTEMPTS``, counted across resends),
then one transaction claims the row and inserts the user. Every failure is the same 400.

Tokens and codes exist in plain form only in memory and in the email; the database keeps hashes and no
log line contains either. Every time comes from the database clock.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from uuid import uuid4

from fastapi import BackgroundTasks, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_result import AuthResult, signed_in_result
from app.core.codes import (
    burn_dummy_check,
    ensure_codes_configured,
    generate_code,
    generate_token,
    hash_code,
    hash_token,
    verify_code,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.password_policy import validate_new_password
from app.core.security import hash_password
from app.integrations.email import mask_email
from app.integrations.email_templates import account_exists_email, registration_email
from app.models.enums import UserRole
from app.schemas.auth import RegisterRequest, RegistrationAccepted, ResendAccepted
from app.services.email_delivery import deliver_quietly

logger = logging.getLogger(__name__)

ACCEPTED_MESSAGE = "If this address can be registered, we have sent a code to it."
INVALID_CODE_MESSAGE = "That code is incorrect or has expired. Request a new code or start again."

SQL_ACCOUNT_EXISTS = text(
    "select exists(select 1 from public.users where lower(email) = :email) as account_exists, now() as db_now"
)
SQL_LOCK_EMAIL = text("select pg_advisory_xact_lock(hashtext(:key))")
SQL_FIND_OPEN = text(
    "select id, cast(role as text) as role, send_count, code_expires_at,"
    "       code_expires_at > now() as code_valid,"
    "       last_sent_at > now() - make_interval(secs => cast(:cooldown as integer)) as in_cooldown,"
    "       greatest(0, ceil(extract(epoch from"
    "           (last_sent_at + make_interval(secs => cast(:cooldown as integer)) - now()))))::integer"
    "           as wait_seconds"
    "  from public.pending_registrations"
    " where email = :email and used_at is null and locked_at is null and expires_at > now()"
    "   for update"
)
SQL_UPDATE_KEEP_CODE = text(
    "update public.pending_registrations"
    "   set name = :name, password_hash = :password_hash, token_hash = :token_hash,"
    "       expires_at = now() + make_interval(mins => cast(:ttl_minutes as integer))"
    " where id = :id"
    " returning code_expires_at"
)
SQL_UPDATE_NEW_CODE = text(
    "update public.pending_registrations"
    "   set name = :name, password_hash = :password_hash, token_hash = :token_hash,"
    "       expires_at = now() + make_interval(mins => cast(:ttl_minutes as integer)),"
    "       code_hash = :code_hash,"
    "       code_expires_at = now() + make_interval(mins => cast(:code_minutes as integer)),"
    "       last_sent_at = now(),"
    "       send_count = send_count + 1"
    " where id = :id"
    " returning code_expires_at"
)
SQL_SUPERSEDE = text("update public.pending_registrations set used_at = now() where email = :email and used_at is null")
SQL_INSERT_PENDING = text(
    "insert into public.pending_registrations"
    " (id, email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at)"
    " values (:id, :email, :name, cast(:role as public.user_role), :password_hash, :token_hash, :code_hash,"
    "         now() + make_interval(mins => cast(:code_minutes as integer)),"
    "         now() + make_interval(mins => cast(:ttl_minutes as integer)))"
    " returning code_expires_at"
)
SQL_COUNT_ATTEMPT = text(
    "update public.pending_registrations set attempts = attempts + 1"
    " where token_hash = :token_hash and used_at is null and locked_at is null"
    "   and expires_at > now() and code_expires_at > now() and attempts < :max_attempts"
    " returning id, code_hash, attempts"
)
SQL_LOCK_ROW = text("update public.pending_registrations set locked_at = now() where id = :id and locked_at is null")
SQL_CLAIM = text(
    "update public.pending_registrations set used_at = now()"
    " where id = :id and used_at is null and locked_at is null and expires_at > now()"
    " returning email, name, cast(role as text) as role, password_hash"
)
SQL_INSERT_USER = text(
    "insert into public.users"
    " (name, email, password_hash, role, email_verified_at, must_change_password, password_changed_at,"
    "  is_super_admin)"
    " values (:name, :email, :password_hash, cast(:role as public.user_role), now(), false, now(), false)"
    " returning id, cast(role as text) as role, is_super_admin, must_change_password"
)
SQL_FIND_FOR_RESEND = text(
    "select id, email from public.pending_registrations"
    " where token_hash = :token_hash and used_at is null and locked_at is null and expires_at > now()"
    "   and send_count < :max_sends"
    "   and last_sent_at <= now() - make_interval(secs => cast(:cooldown as integer))"
    "   for update"
)
SQL_STORE_CODE = text(
    "update public.pending_registrations"
    "   set code_hash = :code_hash,"
    "       code_expires_at = now() + make_interval(mins => cast(:code_minutes as integer)),"
    "       last_sent_at = now(),"
    "       send_count = send_count + 1"
    " where id = :id"
)


class ExistsNoticeThrottle:
    """At most one "account exists" email per address per window, per process (resets on restart).

    Addresses are kept only as SHA-256 digests.
    """

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self.clock = clock
        self._sent: dict[str, float] = {}

    def allow(self, email: str, window_minutes: int) -> bool:
        key = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
        now = self.clock()
        cutoff = now - window_minutes * 60
        for stale in [item for item, sent_at in self._sent.items() if sent_at <= cutoff]:
            del self._sent[stale]
        if key in self._sent:
            return False
        self._sent[key] = now
        return True

    def reset(self) -> None:
        self._sent.clear()


_exists_notices = ExistsNoticeThrottle()


def get_exists_notice_throttle() -> ExistsNoticeThrottle:
    return _exists_notices


def invalid_registration_code() -> AppError:
    """The one response for every verification failure, so no failure can be told apart."""
    return AppError(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_code", INVALID_CODE_MESSAGE)


def _accepted(token: str, email: str, code_expires_at: datetime, resend_available_in: int) -> RegistrationAccepted:
    return RegistrationAccepted(
        message=ACCEPTED_MESSAGE,
        registration_token=token,
        email_masked=mask_email(email),
        code_expires_at=code_expires_at,
        resend_available_in=max(0, int(resend_available_in)),
    )


async def register(
    session: AsyncSession, body: RegisterRequest, role: UserRole | str, background_tasks: BackgroundTasks
) -> RegistrationAccepted:
    """Start (or refresh) a registration; the answer has the same shape whatever the address."""
    ensure_codes_configured()
    settings = get_settings()
    role = UserRole(role)
    # Only rules about the submitted values themselves, so a weak password reveals nothing about accounts.
    validate_new_password(body.password, role, email=body.email, name=body.name)
    # Hashed in every case, so an existing address costs the same work as a new one.
    password_hash = hash_password(body.password)
    token = generate_token()
    cooldown = settings.reg_resend_cooldown_seconds

    existing = (await session.execute(SQL_ACCOUNT_EXISTS, {"email": body.email})).mappings().one()
    if existing["account_exists"]:
        await session.commit()
        if get_exists_notice_throttle().allow(body.email, settings.reg_exists_notice_minutes):
            background_tasks.add_task(deliver_quietly, account_exists_email(body.email))
        logger.info("registration_requested email=%s", mask_email(body.email))
        code_expires_at = existing["db_now"] + timedelta(minutes=settings.reg_code_ttl_minutes)
        return _accepted(token, body.email, code_expires_at, cooldown)

    # Serialises registrations of one address so the one-open-row index is never raced.
    await session.execute(SQL_LOCK_EMAIL, {"key": f"registration:{body.email}"})
    found = await session.execute(SQL_FIND_OPEN, {"email": body.email, "cooldown": cooldown})
    current = found.mappings().first()
    common = {
        "name": body.name,
        "password_hash": password_hash,
        "token_hash": hash_token(token),
        "ttl_minutes": settings.reg_ttl_minutes,
    }
    code: str | None = None
    if current is not None and current["role"] == role.value:
        if (current["in_cooldown"] and current["code_valid"]) or current["send_count"] >= settings.reg_max_sends:
            # Keep the code (and send nothing): it is still fresh, or the send cap is reached.
            result = await session.execute(SQL_UPDATE_KEEP_CODE, {**common, "id": current["id"]})
            resend_available_in = int(current["wait_seconds"] or 0)
        else:
            code = generate_code()
            result = await session.execute(
                SQL_UPDATE_NEW_CODE,
                {
                    **common,
                    "id": current["id"],
                    "code_hash": hash_code("register", current["id"], code),
                    "code_minutes": settings.reg_code_ttl_minutes,
                },
            )
            resend_available_in = cooldown
    else:
        await session.execute(SQL_SUPERSEDE, {"email": body.email})
        pending_id = uuid4()
        code = generate_code()
        result = await session.execute(
            SQL_INSERT_PENDING,
            {
                **common,
                "id": pending_id,
                "email": body.email,
                "role": role.value,
                "code_hash": hash_code("register", pending_id, code),
                "code_minutes": settings.reg_code_ttl_minutes,
            },
        )
        resend_available_in = cooldown
    code_expires_at = result.mappings().one()["code_expires_at"]
    await session.commit()
    logger.info("registration_requested email=%s", mask_email(body.email))
    if code is not None:
        message = registration_email(body.email, code, settings.reg_code_ttl_minutes)
        background_tasks.add_task(deliver_quietly, message)
    return _accepted(token, body.email, code_expires_at, resend_available_in)


async def verify(session: AsyncSession, registration_token: str, code: str) -> AuthResult:
    """Create the account from a valid token and code and sign the new user in."""
    ensure_codes_configured()
    settings = get_settings()

    # Step A: count the attempt in its own committed transaction.
    result = await session.execute(
        SQL_COUNT_ATTEMPT,
        {"token_hash": hash_token(registration_token), "max_attempts": settings.reg_max_attempts},
    )
    attempt = result.mappings().first()
    if attempt is None:
        await session.commit()
        burn_dummy_check("register", code)
        raise invalid_registration_code()
    if not verify_code("register", attempt["id"], code, attempt["code_hash"]):
        if attempt["attempts"] >= settings.reg_max_attempts:
            await session.execute(SQL_LOCK_ROW, {"id": attempt["id"]})
            logger.info("registration_locked pending=%s", attempt["id"])
        await session.commit()
        raise invalid_registration_code()
    await session.commit()

    # Step B: claim the row and create the user together; a unique violation undoes both.
    try:
        result = await session.execute(SQL_CLAIM, {"id": attempt["id"]})
        claimed = result.mappings().first()
        if claimed is None:
            await session.rollback()
            raise invalid_registration_code()
        result = await session.execute(
            SQL_INSERT_USER,
            {
                "name": claimed["name"],
                "email": claimed["email"],
                "password_hash": claimed["password_hash"],
                "role": claimed["role"],
            },
        )
        user = dict(result.mappings().one())
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise invalid_registration_code() from None
    logger.info("registration_completed pending=%s user=%s", attempt["id"], user["id"])
    return signed_in_result(user)


async def resend(session: AsyncSession, registration_token: str, background_tasks: BackgroundTasks) -> ResendAccepted:
    """Send a new code when the row allows it; the answer is the same in every case."""
    ensure_codes_configured()
    settings = get_settings()
    code = generate_code()
    result = await session.execute(
        SQL_FIND_FOR_RESEND,
        {
            "token_hash": hash_token(registration_token),
            "max_sends": settings.reg_max_sends,
            "cooldown": settings.reg_resend_cooldown_seconds,
        },
    )
    current = result.mappings().first()
    if current is None:
        await session.commit()
        burn_dummy_check("register", code)
        return ResendAccepted(message=ACCEPTED_MESSAGE, resend_available_in=settings.reg_resend_cooldown_seconds)
    # The previous code stops working; attempts are deliberately not reset.
    await session.execute(
        SQL_STORE_CODE,
        {
            "id": current["id"],
            "code_hash": hash_code("register", current["id"], code),
            "code_minutes": settings.reg_code_ttl_minutes,
        },
    )
    await session.commit()
    logger.info("registration_code_resent pending=%s", current["id"])
    message = registration_email(current["email"], code, settings.reg_code_ttl_minutes)
    background_tasks.add_task(deliver_quietly, message)
    return ResendAccepted(message=ACCEPTED_MESSAGE, resend_available_in=settings.reg_resend_cooldown_seconds)
