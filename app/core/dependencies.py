"""Authentication, role authorization, and password-change dependencies."""

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "invalid_credentials", "message": "Invalid authentication credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_any(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Validate bearer claims, then load current identity and role from PostgreSQL."""
    if credentials is None:
        raise _unauthorized()
    try:
        claims = decode_access_token(credentials.credentials)
        user_id = UUID(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise _unauthorized() from exc
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or user.is_disabled or _issued_before_password_change(claims, user):
        raise _unauthorized()
    return user


def _issued_before_password_change(claims: dict[str, Any], user: User) -> bool:
    """A token issued before the last password change is revoked.

    ``iat`` has whole-second precision, so it is compared with the whole second of the change: a token
    issued in the same second as the change stays valid.
    """
    changed_at = getattr(user, "password_changed_at", None)
    if not isinstance(changed_at, datetime):
        return False
    issued_at = claims.get("iat")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int | float):
        return True
    return issued_at < math.floor(changed_at.timestamp())


def get_current_user(user: User = Depends(get_current_user_any)) -> User:
    """Require a password change before allowing ordinary protected routes."""
    if user.must_change_password:
        raise HTTPException(
            status_code=403,
            detail={"code": "password_change_required", "message": "Change your password before using this endpoint"},
        )
    return user


def require_role(*roles: UserRole | str) -> Callable[..., Any]:
    """Return a dependency that authorizes using the database-loaded role."""
    allowed = {str(getattr(role, "value", role)) for role in roles}

    def dependency(user: User = Depends(get_current_user)) -> User:
        if str(getattr(user.role, "value", user.role)) not in allowed:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Insufficient role"})
        return user

    return dependency


require_admin = require_role(UserRole.ADMIN)


def require_super_admin(user: User = Depends(require_admin)) -> User:
    """Require an administrator account explicitly marked as super admin."""
    if not user.is_super_admin:
        raise HTTPException(
            status_code=403, detail={"code": "forbidden", "message": "Super administrator access required"}
        )
    return user
