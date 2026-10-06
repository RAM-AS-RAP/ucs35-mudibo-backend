"""Authentication and account credential operations."""

from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password, password_needs_rehash, verify_password
from app.models.enums import UserRole
from app.models.user import User


async def register_user(session: AsyncSession, role: UserRole, fields: dict[str, Any]) -> User:
    """Register a user from explicitly selected public registration fields."""
    user = User(
        name=fields["name"],
        phone_number=fields["phone_number"],
        email=fields.get("email"),
        password_hash=hash_password(fields["password"]),
        role=role,
    )
    session.add(user)
    try:
        await session.commit()
        await session.refresh(user)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "account_exists", "message": "Phone number or email is already registered"}
        ) from exc
    return user


async def login(session: AsyncSession, identifier: str, password: str) -> dict[str, object]:
    """Authenticate with phone or case-insensitive email and return a token."""
    if "@" not in identifier:
        from app.utils.phone import normalize_phone

        try:
            identifier = normalize_phone(identifier)
        except ValueError:
            raise HTTPException(
                status_code=401, detail={"code": "unauthorized", "message": "Invalid identifier or password"}
            ) from None
    user = await session.scalar(
        select(User).where((User.phone_number == identifier) | (func.lower(User.email) == identifier.lower()))
    )
    if user is None or user.is_disabled or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=401, detail={"code": "unauthorized", "message": "Invalid identifier or password"}
        )
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await session.commit()
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "role": user.role.value,
        "must_change_password": user.must_change_password,
    }


async def change_password(session: AsyncSession, user: User, current_password: str, new_password: str) -> User:
    """Verify current credentials, update the hash, and clear forced-change state."""
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(
            status_code=400, detail={"code": "invalid_password", "message": "Current password is incorrect"}
        )
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await session.commit()
    await session.refresh(user)
    return user
