"""Google sign in for farmers and buyers.

1. ``issue_nonce`` stores the SHA-256 of a new ``secrets.token_urlsafe(32)`` nonce (``oauth_nonces``) and
   returns the nonce, which the client passes to Google's sign in initialisation.
2. ``sign_in`` verifies the Google ID token (``app.integrations.google``), compares its ``nonce`` claim
   with the request nonce in constant time and claims the nonce in its own committed transaction, so a
   token can be used once. Then, in one transaction:
   - a Google identity bound to the token's subject signs its user in;
   - otherwise the user with the same (verified) email is signed in and the identity is linked, unless
     the account is an administrator (403 ``google_not_allowed``) or already has another Google
     identity (409 ``google_conflict``);
   - otherwise a single use signup ticket is created and ``role_required`` is returned. No user exists
     yet: ``complete`` creates it once the person has chosen farmer or buyer.
3. ``complete`` claims the ticket and creates the user (``password_set = false``, a random unusable
   password hash, ``email_verified_at = now()``) and the identity in the same transaction.

Every verification failure, a nonce that is unknown, used, expired or does not match, and a disabled
account give the same 401 ``google_sign_in_failed``. The Google email is proven to belong to the
caller, so the other specific answers (admin, conflict) reveal nothing about anyone else.

Google sign in never sends an email code (``LOGIN_OTP_ROLES`` applies to password sign in only) and
issues the same access token as a password sign in. Nonces, tickets and the credential are never logged
or stored in readable form; the Google subject is stored but never logged or returned. Every time comes
from the database clock.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from uuid import UUID

from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_result import (
    AuthResult,
    GoogleAuthResult,
    google_signed_in_result,
    role_required_result,
    signed_in_result,
)
from app.core.codes import generate_token, hash_token
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.integrations.email import mask_email
from app.integrations.google import GoogleIdentity, GoogleUnavailable, GoogleVerificationError, GoogleVerifier
from app.models.identity import GOOGLE_PROVIDER, UserIdentity
from app.schemas.auth import GoogleNonceOut

logger = logging.getLogger(__name__)

SIGN_IN_FAILED_MESSAGE = "Google sign in did not work. Please try again or use your email and password."
UNAVAILABLE_MESSAGE = "Google sign in is not available right now. Please use your email and password."
NOT_ALLOWED_MESSAGE = "Administrator accounts sign in with email and password."
CONFLICT_MESSAGE = (
    "A different Google account is already connected to this email. "
    "Use that Google account, or sign in with your email and password."
)
RETRY_MESSAGE = "Please try signing in with Google again."
TICKET_MESSAGE = "Your Google sign in expired. Please try again."
UNAVAILABLE_RETRY_AFTER = 5

SQL_INSERT_NONCE = text(
    "insert into public.oauth_nonces (nonce_hash, expires_at)"
    " values (:nonce_hash, now() + make_interval(secs => cast(:ttl_seconds as integer)))"
)
SQL_CLAIM_NONCE = text(
    "update public.oauth_nonces set used_at = now()"
    " where nonce_hash = :nonce_hash and used_at is null and expires_at > now()"
    " returning id"
)
SQL_FIND_IDENTITY = text(
    "select i.id as identity_id, u.id, cast(u.role as text) as role, u.is_disabled, u.is_super_admin,"
    "       u.must_change_password"
    "  from public.user_identities as i"
    "  join public.users as u on u.id = i.user_id"
    " where i.provider = 'google' and i.subject = :subject"
)
SQL_TOUCH_IDENTITY = text("update public.user_identities set last_login_at = now(), name = :name where id = :id")
SQL_FIND_USER_BY_EMAIL = text(
    "select id, cast(role as text) as role, is_disabled, is_super_admin, must_change_password, email_verified_at"
    "  from public.users"
    " where lower(email) = :email"
)
SQL_USER_HAS_GOOGLE = text("select id from public.user_identities where user_id = :user_id and provider = 'google'")
SQL_LINK_IDENTITY = text(
    "insert into public.user_identities (user_id, provider, subject, email, email_verified, name, last_login_at)"
    " values (:user_id, 'google', :subject, :email, true, :name, now())"
    " on conflict (provider, subject) do nothing"
    " returning user_id"
)
SQL_LOCK_SIGNUP = text("select pg_advisory_xact_lock(hashtext(:key))")
SQL_SUPERSEDE_SIGNUPS = text(
    "update public.google_signups set used_at = now() where google_sub = :subject and used_at is null"
)
SQL_INSERT_SIGNUP = text(
    "insert into public.google_signups (token_hash, google_sub, email, name, expires_at)"
    " values (:token_hash, :subject, :email, :name, now() + make_interval(mins => cast(:ttl_minutes as integer)))"
    " returning expires_at"
)
SQL_CLAIM_SIGNUP = text(
    "update public.google_signups set used_at = now()"
    " where token_hash = :token_hash and used_at is null and expires_at > now()"
    " returning google_sub, email, name"
)
SQL_INSERT_GOOGLE_USER = text(
    "insert into public.users"
    " (name, email, password_hash, password_set, role, email_verified_at, must_change_password,"
    "  password_changed_at, is_super_admin)"
    " values (:name, :email, :password_hash, false, cast(:role as public.user_role), now(), false, now(), false)"
    " returning id, cast(role as text) as role, is_super_admin, must_change_password"
)
SQL_INSERT_IDENTITY = text(
    "insert into public.user_identities (user_id, provider, subject, email, email_verified, name, last_login_at)"
    " values (:user_id, 'google', :subject, :email, true, :name, now())"
)


def google_unavailable() -> AppError:
    return AppError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "google_unavailable",
        UNAVAILABLE_MESSAGE,
        retry_after=UNAVAILABLE_RETRY_AFTER,
    )


def sign_in_failed() -> AppError:
    """The one 401 for every verification, nonce and disabled account failure."""
    return AppError(status.HTTP_401_UNAUTHORIZED, "google_sign_in_failed", SIGN_IN_FAILED_MESSAGE)


def not_allowed() -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, "google_not_allowed", NOT_ALLOWED_MESSAGE)


def google_conflict() -> AppError:
    return AppError(status.HTTP_409_CONFLICT, "google_conflict", CONFLICT_MESSAGE)


def google_retry() -> AppError:
    return AppError(status.HTTP_409_CONFLICT, "google_retry", RETRY_MESSAGE)


def invalid_ticket() -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_ticket", TICKET_MESSAGE)


def ensure_google_configured() -> None:
    """Raise 503 ``google_unavailable`` when ``GOOGLE_CLIENT_ID`` is empty."""
    if not get_settings().google_configured:
        raise google_unavailable()


def _rejected(reason: str) -> AppError:
    logger.info("google_sign_in_rejected reason=%s", reason)
    return sign_in_failed()


# Nonce -----------------------------------------------------------------------------------------


async def issue_nonce(session: AsyncSession) -> GoogleNonceOut:
    """Store the hash of a new nonce (``GOOGLE_NONCE_TTL_SECONDS``, database clock) and return it."""
    ensure_google_configured()
    settings = get_settings()
    nonce = generate_token()  # secrets.token_urlsafe(32)
    await session.execute(
        SQL_INSERT_NONCE, {"nonce_hash": hash_token(nonce), "ttl_seconds": settings.google_nonce_ttl_seconds}
    )
    await session.commit()
    return GoogleNonceOut(nonce=nonce, expires_in=settings.google_nonce_ttl_seconds)


# Sign in ---------------------------------------------------------------------------------------


async def sign_in(session: AsyncSession, verifier: GoogleVerifier, credential: str, nonce: str) -> GoogleAuthResult:
    """Verify the credential, claim the nonce, then sign in, link, or start a signup; see the docstring."""
    ensure_google_configured()
    try:
        identity = await verifier.verify(credential)
    except GoogleVerificationError:
        raise _rejected("verification") from None
    except GoogleUnavailable:
        raise google_unavailable() from None
    if not hmac.compare_digest(identity.nonce.encode("utf-8"), nonce.encode("utf-8")):
        raise _rejected("nonce_mismatch")

    # The nonce is claimed in its own committed transaction: a replayed token always fails.
    claimed = (await session.execute(SQL_CLAIM_NONCE, {"nonce_hash": hash_token(nonce)})).mappings().first()
    await session.commit()
    if claimed is None:
        raise _rejected("nonce_unusable")

    linked = (await session.execute(SQL_FIND_IDENTITY, {"subject": identity.subject})).mappings().first()
    if linked is not None:
        return await _sign_in_linked(session, linked, identity)
    user = (await session.execute(SQL_FIND_USER_BY_EMAIL, {"email": identity.email})).mappings().first()
    if user is not None:
        return await _link_existing(session, user, identity)
    return await _start_signup(session, identity)


async def _sign_in_linked(session: AsyncSession, row, identity: GoogleIdentity) -> GoogleAuthResult:
    if row["is_disabled"]:
        await session.rollback()
        raise _rejected("disabled")
    if row["role"] == "admin":
        await session.rollback()
        raise not_allowed()
    await session.execute(SQL_TOUCH_IDENTITY, {"id": row["identity_id"], "name": identity.name})
    await session.commit()
    logger.info("google_sign_in user=%s", row["id"])
    return google_signed_in_result(dict(row))


async def _link_existing(session: AsyncSession, user, identity: GoogleIdentity) -> GoogleAuthResult:
    """Sign in the account with the same verified email and bind this Google subject to it."""
    if user["role"] == "admin":
        await session.rollback()
        logger.info("google_sign_in_refused_admin user=%s", user["id"])
        raise not_allowed()
    if user["is_disabled"]:
        await session.rollback()
        raise _rejected("disabled")
    if user["email_verified_at"] is None:  # cannot happen (the column is required); refuse anyway
        await session.rollback()
        raise _rejected("email_not_verified")
    other = (await session.execute(SQL_USER_HAS_GOOGLE, {"user_id": user["id"]})).mappings().first()
    if other is not None:
        await session.rollback()
        logger.info("google_identity_conflict user=%s", user["id"])
        raise google_conflict()
    try:
        result = await session.execute(
            SQL_LINK_IDENTITY,
            {"user_id": user["id"], "subject": identity.subject, "email": identity.email, "name": identity.name},
        )
        inserted = result.mappings().first()
    except IntegrityError:
        # (user_id, provider) is unique: another Google identity was linked to this user meanwhile.
        await session.rollback()
        logger.info("google_identity_conflict user=%s", user["id"])
        raise google_conflict() from None
    if inserted is None:
        # The same Google subject was linked by a concurrent sign in; continue as a linked sign in.
        await session.rollback()
        linked = (await session.execute(SQL_FIND_IDENTITY, {"subject": identity.subject})).mappings().first()
        if linked is None:
            await session.rollback()
            raise _rejected("link_race")
        return await _sign_in_linked(session, linked, identity)
    await session.commit()
    logger.info("google_identity_linked user=%s", user["id"])
    return google_signed_in_result(dict(user))


async def _start_signup(session: AsyncSession, identity: GoogleIdentity) -> GoogleAuthResult:
    """No account has this email: create a single use ticket and ask for the role."""
    settings = get_settings()
    token = generate_token()
    try:
        # Serialises sign ins of one new Google account so the one-open-ticket index is never raced.
        await session.execute(SQL_LOCK_SIGNUP, {"key": f"google-signup:{identity.subject}"})
        await session.execute(SQL_SUPERSEDE_SIGNUPS, {"subject": identity.subject})
        result = await session.execute(
            SQL_INSERT_SIGNUP,
            {
                "token_hash": hash_token(token),
                "subject": identity.subject,
                "email": identity.email,
                "name": identity.name,
                "ttl_minutes": settings.google_signup_ttl_minutes,
            },
        )
        signup_expires_at = result.mappings().one()["expires_at"]
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise google_retry() from None
    logger.info("google_signup_started email=%s", mask_email(identity.email))
    return role_required_result(
        signup_token=token, email=identity.email, name=identity.name, signup_expires_at=signup_expires_at
    )


# Role choice -----------------------------------------------------------------------------------


async def complete(session: AsyncSession, signup_token: str, role: str) -> AuthResult:
    """Claim the ticket and create the farmer or buyer with the Google identity, in one transaction."""
    ensure_google_configured()
    if role not in ("farmer", "buyer"):  # the schema already allows only these two (422 otherwise)
        raise not_allowed()
    # A random password nobody knows (never stored or logged in plain form); password_set stays false.
    password_hash = hash_password(secrets.token_urlsafe(48))
    try:
        claimed = (await session.execute(SQL_CLAIM_SIGNUP, {"token_hash": hash_token(signup_token)})).mappings().first()
        if claimed is None:
            await session.rollback()
            raise invalid_ticket()
        result = await session.execute(
            SQL_INSERT_GOOGLE_USER,
            {"name": claimed["name"], "email": claimed["email"], "password_hash": password_hash, "role": role},
        )
        user = dict(result.mappings().one())
        await session.execute(
            SQL_INSERT_IDENTITY,
            {
                "user_id": user["id"],
                "subject": claimed["google_sub"],
                "email": claimed["email"],
                "name": claimed["name"],
            },
        )
        await session.commit()
    except IntegrityError:
        # The email (or the Google subject) got an account in the meantime. Rolling back also un-claims
        # the ticket; signing in with Google again will now find and link that account.
        await session.rollback()
        logger.info("google_signup_retry")
        raise google_retry() from None
    logger.info("google_signup_completed user=%s role=%s", user["id"], role)
    return signed_in_result(user)


# Profile ---------------------------------------------------------------------------------------


async def is_google_linked(session: AsyncSession, user_id: UUID) -> bool:
    """Whether the user has a Google identity (one indexed query, used by ``GET /auth/me``)."""
    found = await session.scalar(
        select(UserIdentity.id)
        .where(UserIdentity.user_id == user_id, UserIdentity.provider == GOOGLE_PROVIDER)
        .limit(1)
    )
    return found is not None
