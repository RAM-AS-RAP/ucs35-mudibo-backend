"""Authentication, registration, password-change, and login-throttle services."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, InvalidCredentials, TooManyAttempts
from app.core.password_policy import validate_new_password
from app.core.security import DUMMY_HASH, create_access_token, hash_password, needs_rehash, verify_password
from app.models.enums import UserRole
from app.models.user import User
from app.utils.phone import normalize_phone


class LoginThrottle:
    """Per-process identifier throttle; its counters reset on process restart."""

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
        """Add a failed attempt to this identifier's sliding window."""
        key = self._key(identifier)
        attempts = self._active_failures(key)
        if key not in self._failures:
            attempts = deque()
            self._failures[key] = attempts
        attempts.append(self.clock())

    def clear(self, identifier: str) -> None:
        """Clear failures after a successful authentication."""
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


def _field(data: Any, name: str) -> Any:
    if isinstance(data, Mapping):
        return data[name]
    return getattr(data, name)


async def register_user(session: AsyncSession, data: Any, role: UserRole | str) -> User:
    """Create a user from a fixed set of fields and a role supplied by the route."""
    email = _field(data, "email")
    user = User(
        name=_field(data, "name"),
        phone_number=normalize_phone(_field(data, "phone_number")),
        email=str(email).lower() if email is not None else None,
        password_hash=hash_password(_field(data, "password")),
        role=UserRole(role),
    )
    session.add(user)
    try:
        await session.commit()
        await session.refresh(user)
    except IntegrityError as exc:
        await session.rollback()
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if sqlstate == "23505" or "unique constraint" in str(exc).lower():
            raise ConflictError() from exc
        raise
    return user


async def find_user_by_identifier(session: AsyncSession, identifier: str) -> User | None:
    """Find a user by case-insensitive email or normalized Kenyan phone."""
    value = identifier.strip()
    if "@" in value:
        return await session.scalar(select(User).where(func.lower(User.email) == value.lower()))
    try:
        phone_number = normalize_phone(value)
    except ValueError:
        return None
    return await session.scalar(select(User).where(User.phone_number == phone_number))


async def authenticate(session: AsyncSession, identifier: str, password: str) -> User:
    """Authenticate with indistinguishable errors and equalized missing-user work."""
    user = await find_user_by_identifier(session, identifier)
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


async def login(session: AsyncSession, identifier: str, password: str) -> dict[str, object]:
    """Authenticate with identifier throttling and return a short-lived bearer token."""
    throttle = get_login_throttle()
    key = identifier.strip().lower()
    if throttle.is_blocked(key):
        raise TooManyAttempts()
    try:
        user = await authenticate(session, identifier, password)
    except InvalidCredentials:
        throttle.record_failure(key)
        raise
    throttle.clear(key)
    settings = get_settings()
    return {
        "access_token": create_access_token(user.id, user.role),
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
        "role": user.role.value,
        "is_super_admin": bool(user.is_super_admin),
        "must_change_password": user.must_change_password,
    }


async def change_password(session: AsyncSession, user: User, current_password: str, new_password: str) -> None:
    """Verify and replace the current password, clearing forced-change state.

    ``password_changed_at`` (database clock) revokes every access token issued before the change,
    including the one used for this request, so the user must log in again.
    """
    if not verify_password(user.password_hash, current_password):
        raise HTTPException(
            status_code=400,
            detail={"code": "wrong_current_password", "message": "Current password is incorrect"},
        )
    validate_new_password(new_password, user.role, email=user.email, name=user.name, phone=user.phone_number)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = func.now()
    await session.commit()
