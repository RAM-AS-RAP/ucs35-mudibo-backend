"""Password policy for invite acceptance, password reset and password change.

Admins need at least 12 characters, farmers and buyers at least 8, everyone at most 128. A password
may not equal the account's email, the part of the email before ``@``, the name or the phone number
(case-insensitive), and may not be one of the common passwords in ``common_passwords.txt``.
Registration keeps its own rules in ``app.schemas.auth``.
"""

from functools import lru_cache
from pathlib import Path

from fastapi import status

from app.core.errors import AppError
from app.models.enums import UserRole

ADMIN_MIN_LENGTH = 12
USER_MIN_LENGTH = 8
MAX_LENGTH = 128
_COMMON_PASSWORDS_FILE = Path(__file__).with_name("common_passwords.txt")


@lru_cache
def common_passwords() -> frozenset[str]:
    lines = _COMMON_PASSWORDS_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip())


def _too_weak(reason: str) -> AppError:
    return AppError(
        status.HTTP_422_UNPROCESSABLE_CONTENT, "password_too_weak", "The password is too weak.", reason=reason
    )


def minimum_length(role: UserRole | str | None) -> int:
    value = str(getattr(role, "value", role)) if role is not None else None
    return ADMIN_MIN_LENGTH if value == UserRole.ADMIN.value else USER_MIN_LENGTH


def validate_new_password(
    password: str,
    role: UserRole | str | None,
    *,
    email: str | None = None,
    name: str | None = None,
    phone: str | None = None,
) -> None:
    """Raise 422 ``password_too_weak`` with a short reason; the reason never echoes the password."""
    minimum = minimum_length(role)
    if len(password) < minimum:
        raise _too_weak(f"Use at least {minimum} characters.")
    if len(password) > MAX_LENGTH:
        raise _too_weak(f"Use at most {MAX_LENGTH} characters.")
    lowered = password.strip().lower()
    personal: set[str] = set()
    if email:
        personal.add(email.strip().lower())
        personal.add(email.strip().lower().split("@", 1)[0])
    if name:
        personal.add(name.strip().lower())
    if phone:
        personal.add(phone.strip().lower())
    personal.discard("")
    if lowered in personal:
        raise _too_weak("The password must not be your email, name or phone number.")
    if lowered in common_passwords():
        raise _too_weak("This password is too common.")
