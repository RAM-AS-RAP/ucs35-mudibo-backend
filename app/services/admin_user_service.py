"""Administrator user management: create admins, list, disable/enable, and reset passwords.

User rows are locked alone (``SELECT ... FOR UPDATE`` on the target). Logs carry only the event name,
the acting admin id and the target id: never passwords, temporary passwords, hashes or tokens.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.errors import AppError, ConflictError
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.user import User
from app.schemas.admin import CreateAdminRequest, CreateAdminResponse, UserAdminOut, UserAdminPage
from app.schemas.common import Pagination
from app.utils.search import LIKE_ESCAPE, contains_pattern, phone_prefix

logger = logging.getLogger(__name__)


def _role(user: Any) -> str:
    return str(getattr(user.role, "value", user.role))


def _is_super_admin(user: Any) -> bool:
    return bool(getattr(user, "is_super_admin", False))


def generate_temporary_password() -> str:
    """A server-generated temporary password; it is returned once and only its hash is stored."""
    return secrets.token_urlsafe(get_settings().admin_temp_password_bytes)


def _users_query() -> Select[Any]:
    """Users with creator/disabler names and grouped listing/order counts, in one statement."""
    disabler = aliased(User)
    creator = aliased(User)
    listing_counts = (
        select(ProduceListing.farmer_id.label("user_id"), func.count().label("n"))
        .group_by(ProduceListing.farmer_id)
        .subquery()
    )
    order_counts = select(Order.buyer_id.label("user_id"), func.count().label("n")).group_by(Order.buyer_id).subquery()
    return (
        select(
            User,
            disabler.name.label("disabled_by_name"),
            creator.name.label("created_by_name"),
            listing_counts.c.n.label("listing_count"),
            order_counts.c.n.label("order_count"),
        )
        .outerjoin(disabler, disabler.id == User.disabled_by)
        .outerjoin(creator, creator.id == User.created_by)
        .outerjoin(listing_counts, listing_counts.c.user_id == User.id)
        .outerjoin(order_counts, order_counts.c.user_id == User.id)
        .execution_options(populate_existing=True)
    )


def _user_out(row: Any) -> UserAdminOut:
    user, disabled_by_name, created_by_name, listing_count, order_count = row
    role = _role(user)
    return UserAdminOut(
        id=user.id,
        name=user.name,
        phone_number=user.phone_number,
        email=user.email,
        role=role,
        is_super_admin=user.is_super_admin,
        is_disabled=user.is_disabled,
        disabled_at=user.disabled_at,
        disabled_by_name=disabled_by_name,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        created_by_name=created_by_name,
        listing_count=int(listing_count or 0) if role == UserRole.FARMER else None,
        order_count=int(order_count or 0) if role == UserRole.BUYER else None,
    )


async def get_user_out(session: AsyncSession, user_id: UUID) -> UserAdminOut:
    row = (await session.execute(_users_query().where(User.id == user_id))).first()
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "user_not_found", "User not found.")
    return _user_out(row)


def _search_condition(q: str) -> Any:
    pattern = contains_pattern(q)
    conditions = [User.name.ilike(pattern, escape=LIKE_ESCAPE), User.email.ilike(pattern, escape=LIKE_ESCAPE)]
    digits = phone_prefix(q)
    if digits is not None:
        conditions.append(User.phone_number.like(f"{digits}%"))
    return or_(*conditions)


async def list_users(
    session: AsyncSession,
    pagination: Pagination,
    *,
    role: UserRole | None = None,
    is_disabled: bool | None = None,
    q: str | None = None,
) -> UserAdminPage:
    """Users newest first, with counts from grouped subqueries: one count and one page statement."""
    conditions: list[Any] = []
    if role is not None:
        conditions.append(User.role == role)
    if is_disabled is not None:
        conditions.append(User.is_disabled.is_(is_disabled))
    if q is not None:
        conditions.append(_search_condition(q))
    total = int(await session.scalar(select(func.count()).select_from(User).where(*conditions)) or 0)
    rows = (
        await session.execute(
            _users_query()
            .where(*conditions)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()
    return UserAdminPage(
        items=[_user_out(row) for row in rows], total=total, limit=pagination.limit, offset=pagination.offset
    )


async def create_admin(session: AsyncSession, actor: User, body: CreateAdminRequest) -> CreateAdminResponse:
    """Create an ordinary admin with a server-generated temporary password that must be changed."""
    temporary_password = generate_temporary_password()
    admin = User(
        id=uuid4(),
        name=body.name,
        phone_number=body.phone_number,
        email=body.email,
        password_hash=hash_password(temporary_password),
        role=UserRole.ADMIN,
        is_super_admin=False,
        must_change_password=True,
        created_by=actor.id,
    )
    session.add(admin)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if sqlstate == "23505" or "unique" in str(original).lower():
            # The same generic conflict as registration, so phone and email cannot be told apart.
            raise ConflictError() from None
        raise
    logger.info("admin_created actor=%s target=%s", actor.id, admin.id)
    out = await get_user_out(session, admin.id)
    return CreateAdminResponse(**out.model_dump(), temporary_password=temporary_password)


async def _lock_target(session: AsyncSession, user_id: UUID) -> User:
    target = await session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    if target is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "user_not_found", "User not found.")
    return target


def _forbid_admin_target_for_ordinary_admin(actor: User, target: User) -> None:
    if _role(target) == UserRole.ADMIN and not _is_super_admin(actor):
        raise AppError(status.HTTP_403_FORBIDDEN, "forbidden", "Only the super administrator can manage admins.")


async def set_user_disabled(session: AsyncSession, actor: User, user_id: UUID, disabled: bool) -> UserAdminOut:
    """Disable or enable an account; repeating the call in the same state changes nothing."""
    target = await _lock_target(session, user_id)
    if target.is_super_admin:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "cannot_modify_super_admin", "The super administrator cannot be modified."
        )
    if target.id == actor.id:
        raise AppError(status.HTTP_403_FORBIDDEN, "cannot_disable_self", "You cannot disable or enable yourself.")
    _forbid_admin_target_for_ordinary_admin(actor, target)

    if target.is_disabled != disabled:
        # The database check ties is_disabled to disabled_at, so the three fields change together.
        target.is_disabled = disabled
        target.disabled_at = func.now() if disabled else None
        target.disabled_by = actor.id if disabled else None
        await session.commit()
        logger.info("%s actor=%s target=%s", "user_disabled" if disabled else "user_enabled", actor.id, target.id)
    else:
        await session.commit()  # release the row lock
    return await get_user_out(session, user_id)


async def reset_password(session: AsyncSession, actor: User, user_id: UUID) -> str:
    """Replace the password with a temporary one and force a change at next login.

    Existing access tokens stay valid until they expire, because tokens carry no password version.
    """
    target = await _lock_target(session, user_id)
    if target.is_super_admin:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "cannot_modify_super_admin", "The super administrator cannot be modified."
        )
    _forbid_admin_target_for_ordinary_admin(actor, target)
    temporary_password = generate_temporary_password()
    target.password_hash = hash_password(temporary_password)
    target.must_change_password = True
    await session.commit()
    logger.info("password_reset actor=%s target=%s", actor.id, target.id)
    return temporary_password
