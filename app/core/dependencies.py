"""Authentication, role authorization, and password-change dependencies."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(token: str = Depends(oauth2_scheme), session: AsyncSession = Depends(get_db)) -> User:
    """Decode identity, then reload account and role from the database each request."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Invalid authentication credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = decode_token(token)
        user_id = UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise unauthorized from exc
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or user.is_disabled:
        raise unauthorized
    return user


def require_role(*roles: UserRole | str) -> Callable[..., Any]:
    """Return a dependency that allows only current database roles."""
    allowed = {str(getattr(role, "value", role)) for role in roles}

    def dependency(user: User = Depends(get_current_user)) -> User:
        if str(getattr(user.role, "value", user.role)) not in allowed:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Insufficient role"})
        return user

    return dependency


def require_super_admin(user: User = Depends(require_role(UserRole.ADMIN))) -> User:
    """Require an administrator account explicitly marked as super admin."""
    if not user.is_super_admin:
        raise HTTPException(
            status_code=403, detail={"code": "forbidden", "message": "Super administrator access required"}
        )
    return user


def must_change_password(request: Request, user: User = Depends(get_current_user)) -> User:
    """Block protected operations until a temporary password has been changed."""
    permitted = {"/api/v1/auth/change-password", "/api/v1/auth/me"}
    if user.must_change_password and request.url.path not in permitted:
        raise HTTPException(
            status_code=403,
            detail={"code": "password_change_required", "message": "Change your password before using this endpoint"},
        )
    return user
