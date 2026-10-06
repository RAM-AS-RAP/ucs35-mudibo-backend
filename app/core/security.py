"""Password hashing and signed access-token helpers."""

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.core.config import get_settings

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password and transparently refresh stale Argon2 parameters."""
    try:
        valid = _password_hasher.verify(password_hash, password)
    except (VerificationError, TypeError, ValueError):
        return False
    return bool(valid)


def password_needs_rehash(password_hash: str) -> bool:
    """Report whether the configured Argon2 parameters have changed."""
    return _password_hasher.check_needs_rehash(password_hash)


def create_access_token(user: Any) -> str:
    """Create a signed token containing only identity, role, and expiry claims."""
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "role": str(getattr(user.role, "value", user.role)),
            "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode a token, raising PyJWT errors for invalid or expired tokens."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def constant_time_equal(left: str, right: str) -> bool:
    """Compare secrets without leaking content through timing differences."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
