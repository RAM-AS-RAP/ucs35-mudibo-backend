"""Password sign in with an emailed second step, the login throttle, and password change.

Sign in has two steps for the roles in ``LOGIN_OTP_ROLES``:

1. ``login`` checks the email and password. A wrong password, an unknown email and a disabled account
   give the same 401. Only after a correct password is a login challenge created (superseding the
   user's active one) and its code emailed. The response carries the challenge token, never a code.
2. ``verify_login`` takes the challenge token and the code. The attempt is counted in its own committed
   transaction before the comparison (locking the challenge at ``LOGIN_MAX_ATTEMPTS``), then one
   transaction claims the challenge and the user gets an access token.

The challenge token alone is useless without the code and the code alone is useless without the token.
Tokens and codes are stored only as hashes and never logged. Roles not in ``LOGIN_OTP_ROLES`` get the
access token directly from ``login``. The challenge tables are queried with SQL text; every time comes
from the database clock.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_result import AuthResult, code_required_result, signed_in_result
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
from app.core.errors import AppError, InvalidCredentials, TooManyAttempts
from app.core.password_policy import validate_new_password
from app.core.security import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.integrations.email import mask_email
from app.integrations.email_templates import login_code_email
from app.models.user import User
from app.schemas.auth import LoginResendOut
from app.services.email_delivery import deliver_within_timeout

logger = logging.getLogger(__name__)

LOGIN_CODE_INVALID_MESSAGE = "That code is incorrect or has expired."
CHALLENGE_CLOSED_MESSAGE = "Start again from the sign in screen."

# Login challenge statements. Kept as module constants so tests can recognise them.
SQL_LOCK_USER_CHALLENGES = text("select pg_advisory_xact_lock(hashtext(:key))")
SQL_RECENT_CHALLENGES = text(
    "select count(*) as recent,"
    "       ceil(extract(epoch from (min(created_at) + interval '1 hour' - now())))::integer as retry_after"
    "  from public.login_challenges"
    " where user_id = :user_id and created_at > now() - interval '1 hour'"
)
SQL_SUPERSEDE_CHALLENGES = text(
    "update public.login_challenges set used_at = now() where user_id = :user_id and used_at is null"
)
SQL_INSERT_CHALLENGE = text(
    "insert into public.login_challenges (id, user_id, token_hash, code_hash, code_expires_at, expires_at)"
    " values (:id, :user_id, :token_hash, :code_hash,"
    "         now() + make_interval(mins => cast(:code_minutes as integer)),"
    "         now() + make_interval(mins => cast(:challenge_minutes as integer)))"
    " returning code_expires_at"
)
SQL_COUNT_LOGIN_ATTEMPT = text(
    "update public.login_challenges set attempts = attempts + 1"
    " where token_hash = :token_hash and used_at is null and locked_at is null"
    "   and expires_at > now() and code_expires_at > now() and attempts < :max_attempts"
    " returning id, user_id, code_hash, attempts"
)
SQL_LOCK_CHALLENGE = text("update public.login_challenges set locked_at = now() where id = :id and locked_at is null")
SQL_CLAIM_CHALLENGE = text(
    "update public.login_challenges set used_at = now()"
    " where id = :id and used_at is null and locked_at is null and expires_at > now()"
    " returning user_id"
)
SQL_FIND_CHALLENGE_FOR_RESEND = text(
    "select c.id, c.user_id, c.send_count, u.email,"
    "       greatest(0, ceil(extract(epoch from"
    "           (c.last_sent_at + make_interval(secs => cast(:cooldown as integer)) - now()))))::integer"
    "           as wait_seconds"
    "  from public.login_challenges as c"
    "  join public.users as u on u.id = c.user_id"
    " where c.token_hash = :token_hash and c.used_at is null and c.locked_at is null"
    "   and c.expires_at > now() and u.is_disabled = false"
    "   for update of c"
)
SQL_STORE_LOGIN_CODE = text(
    "update public.login_challenges"
    "   set code_hash = :code_hash,"
    "       code_expires_at = least(now() + make_interval(mins => cast(:code_minutes as integer)), expires_at),"
    "       last_sent_at = now(),"
    "       send_count = send_count + 1"
    " where id = :id"
    " returning code_expires_at"
)


class LoginThrottle:
    """Per-process email throttle; its counters reset on process restart."""

    def __init__(self, max_failures: int = 5, window_minutes: int = 15, clock: Callable[[], float] = monotonic) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_minutes * 60
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _key(self, identifier: str) -> str:
        return identifier.strip().lower()

    def _active_failures(self, identifier: str) -> deque[float]:
        key = self._key(identifier)
        attempts = self._failures.setdefault(key, deque())
        cutoff = self.clock() - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._failures.pop(key, None)
            return deque()
        return attempts

    def is_blocked(self, identifier: str) -> bool:
        """Return whether enough recent failures block another attempt."""
        return len(self._active_failures(identifier)) >= self.max_failures

    def record_failure(self, identifier: str) -> None:
        """Add a failed attempt to this email's sliding window."""
        key = self._key(identifier)
        attempts = self._active_failures(key)
        if key not in self._failures:
            attempts = deque()
            self._failures[key] = attempts
        attempts.append(self.clock())

    def clear(self, identifier: str) -> None:
        """Clear failures after a correct password."""
        self._failures.pop(self._key(identifier), None)

    def reset(self) -> None:
        """Clear all counters, primarily for isolated tests."""
        self._failures.clear()


_login_throttle: LoginThrottle | None = None


def get_login_throttle() -> LoginThrottle:
    """Return the per-process throttle configured from runtime settings."""
    global _login_throttle
    settings = get_settings()
    if (
        _login_throttle is None
        or _login_throttle.max_failures != settings.login_max_failures
        or _login_throttle.window_seconds != settings.login_failure_window_minutes * 60
    ):
        _login_throttle = LoginThrottle(settings.login_max_failures, settings.login_failure_window_minutes)
    return _login_throttle


def challenge_closed() -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, "challenge_closed", CHALLENGE_CLOSED_MESSAGE)


def login_code_invalid() -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_code", LOGIN_CODE_INVALID_MESSAGE)


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Find a user by case-insensitive email (emails are stored in lower case)."""
    return await session.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Authenticate with indistinguishable errors and equalized missing-user work."""
    user = await find_user_by_email(session, email)
    if user is None:
        verify_password(DUMMY_HASH, password)
        raise InvalidCredentials()
    if not verify_password(user.password_hash, password):
        raise InvalidCredentials()
    if user.is_disabled:
        raise InvalidCredentials()
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await session.commit()
    return user


def requires_login_code(user: Any) -> bool:
    """Whether this user's role must enter an emailed code after the password."""
    role = getattr(user.role, "value", user.role)
    return str(role) in get_settings().login_otp_role_set


@dataclass(frozen=True)
class IssuedChallenge:
    """A new login challenge; ``token`` and ``code`` exist in memory only and are never stored or logged."""

    challenge_id: Any
    token: str
    code: str
    code_expires_at: datetime


async def issue_login_challenge(
    session: AsyncSession, user: Any, *, enforce_hourly_limit: bool = True
) -> IssuedChallenge:
    """Supersede the user's active challenge, create a new one and commit. Sends nothing.

    Serialised per user with a transaction scoped advisory lock (a bound key), so two sign ins at the
    same moment cannot both hold an active challenge. ``enforce_hourly_limit`` is turned off only by the
    operator recovery script.
    """
    ensure_codes_configured()
    settings = get_settings()
    await session.execute(SQL_LOCK_USER_CHALLENGES, {"key": f"login-challenge:{user.id}"})
    if enforce_hourly_limit:
        recent = (await session.execute(SQL_RECENT_CHALLENGES, {"user_id": user.id})).mappings().one()
        if int(recent["recent"] or 0) >= settings.login_challenges_per_hour:
            await session.rollback()
            raise AppError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too_many_login_codes",
                "Too many sign in codes were requested. Try again later.",
                retry_after=max(1, int(recent["retry_after"] or 1)),
            )
    await session.execute(SQL_SUPERSEDE_CHALLENGES, {"user_id": user.id})
    challenge_id = uuid4()
    token = generate_token()
    code = generate_code()
    result = await session.execute(
        SQL_INSERT_CHALLENGE,
        {
            "id": challenge_id,
            "user_id": user.id,
            "token_hash": hash_token(token),
            "code_hash": hash_code("login", challenge_id, code),
            "code_minutes": settings.login_code_ttl_minutes,
            "challenge_minutes": settings.login_challenge_ttl_minutes,
        },
    )
    code_expires_at = result.mappings().one()["code_expires_at"]
    await session.commit()
    logger.info("login_challenge_created user=%s", user.id)
    return IssuedChallenge(challenge_id, token, code, code_expires_at)


async def login(session: AsyncSession, email: str, password: str) -> AuthResult:
    """Check the password (with the per email throttle), then sign in or start the code step."""
    throttle = get_login_throttle()
    key = email.strip().lower()
    if throttle.is_blocked(key):
        raise TooManyAttempts()
    try:
        user = await authenticate(session, email, password)
    except InvalidCredentials:
        throttle.record_failure(key)
        raise
    throttle.clear(key)
    if not requires_login_code(user):
        return signed_in_result(user)

    settings = get_settings()
    issued = await issue_login_challenge(session, user)
    # Sent after the commit, with no transaction open; a failed send keeps the challenge for a resend.
    message = login_code_email(user.email, issued.code, settings.login_code_ttl_minutes)
    code_sent = await deliver_within_timeout(message)
    return code_required_result(
        challenge_token=issued.token,
        email_masked=mask_email(user.email),
        code_expires_at=issued.code_expires_at,
        resend_available_in=settings.login_resend_cooldown_seconds,
        code_sent=code_sent,
    )


async def verify_login(session: AsyncSession, challenge_token: str, code: str) -> AuthResult:
    """Exchange a challenge token and its code for an access token; see the module docstring."""
    ensure_codes_configured()
    settings = get_settings()

    # Step A: count the attempt in its own committed transaction.
    result = await session.execute(
        SQL_COUNT_LOGIN_ATTEMPT,
        {"token_hash": hash_token(challenge_token), "max_attempts": settings.login_max_attempts},
    )
    attempt = result.mappings().first()
    if attempt is None:
        await session.commit()
        burn_dummy_check("login", code)
        raise challenge_closed()
    if not verify_code("login", attempt["id"], code, attempt["code_hash"]):
        if attempt["attempts"] >= settings.login_max_attempts:
            await session.execute(SQL_LOCK_CHALLENGE, {"id": attempt["id"]})
            logger.info("login_challenge_locked user=%s", attempt["user_id"])
        await session.commit()
        raise login_code_invalid()
    await session.commit()

    # Step B: claim the challenge, then load the user as it is now.
    result = await session.execute(SQL_CLAIM_CHALLENGE, {"id": attempt["id"]})
    claimed = result.mappings().first()
    if claimed is None:
        await session.rollback()
        raise challenge_closed()
    await session.commit()
    user = await session.scalar(
        select(User).where(User.id == claimed["user_id"]).execution_options(populate_existing=True)
    )
    if user is None or user.is_disabled:
        raise challenge_closed()
    logger.info("login_verified user=%s", user.id)
    return signed_in_result(user)


async def resend_login_code(session: AsyncSession, challenge_token: str) -> LoginResendOut:
    """Replace the code of an active challenge; the old code stops working and attempts are kept."""
    ensure_codes_configured()
    settings = get_settings()
    result = await session.execute(
        SQL_FIND_CHALLENGE_FOR_RESEND,
        {"token_hash": hash_token(challenge_token), "cooldown": settings.login_resend_cooldown_seconds},
    )
    current = result.mappings().first()
    if current is None:
        await session.rollback()
        raise challenge_closed()
    wait_seconds = int(current["wait_seconds"] or 0)
    if wait_seconds > 0:
        await session.rollback()
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "resend_cooldown",
            "Wait a moment before requesting another code.",
            retry_after=wait_seconds,
        )
    if current["send_count"] >= settings.login_max_sends:
        await session.rollback()
        raise AppError(
            status.HTTP_409_CONFLICT,
            "send_limit_reached",
            "No more codes can be sent for this sign in. Start again from the sign in screen.",
        )
    code = generate_code()
    result = await session.execute(
        SQL_STORE_LOGIN_CODE,
        {
            "id": current["id"],
            "code_hash": hash_code("login", current["id"], code),
            "code_minutes": settings.login_code_ttl_minutes,
        },
    )
    code_expires_at = result.mappings().one()["code_expires_at"]
    await session.commit()
    logger.info("login_code_resent user=%s", current["user_id"])
    code_sent = await deliver_within_timeout(login_code_email(current["email"], code, settings.login_code_ttl_minutes))
    return LoginResendOut(
        code_sent=code_sent,
        resend_available_in=settings.login_resend_cooldown_seconds,
        code_expires_at=code_expires_at,
    )


async def change_password(session: AsyncSession, user: User, current_password: str, new_password: str) -> None:
    """Verify and replace the current password, clearing forced-change state.

    ``password_changed_at`` (database clock) revokes every access token issued before the change,
    including the one used for this request, so the user must sign in again.
    """
    if not verify_password(user.password_hash, current_password):
        raise HTTPException(
            status_code=400,
            detail={"code": "wrong_current_password", "message": "Current password is incorrect"},
        )
    validate_new_password(new_password, user.role, email=user.email, name=user.name)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = func.now()
    await session.commit()
