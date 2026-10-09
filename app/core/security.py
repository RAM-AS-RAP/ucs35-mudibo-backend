"""Password hashing and signed access-token helpers."""

import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.core.config import get_settings

_password_hasher = PasswordHasher()
DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(48))


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify a password hash without allowing verification errors to escape."""
    try:
        valid = _password_hasher.verify(password_hash, password)
    except (VerificationError, TypeError, ValueError):
        return False
    return bool(valid)


def needs_rehash(password_hash: str) -> bool:
    """Report whether a valid hash uses outdated Argon2 parameters."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except (VerificationError, TypeError, ValueError):
        return False


def create_access_token(user_id: str | UUID, role: str | Any) -> str:
    """Create a signed token with user identity, role, issue time, and expiry."""
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": str(getattr(role, "value", role)),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode a signed token and raise PyJWT's typed error for invalid claims."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError:
        raise
    except (TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("Invalid access token") from exc
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise jwt.InvalidTokenError("Access token subject is required")
    return claims


def safe_compare(left: str, right: str) -> bool:
    """Compare secrets without leaking content through timing differences."""
    return hmac.compare_digest(left, right)


password_needs_rehash = needs_rehash
decode_token = decode_access_token
constant_time_equal = safe_compare
