"""Administrator user management: list users, disable and enable accounts.

Admins are created through email invites (``invite_service``) and passwords are reset by the user
through the email reset flow (``password_reset_service``); no function here produces a credential.
User rows are locked alone (``SELECT ... FOR UPDATE`` on the target). Logs carry only the event name,
the acting admin id and the target id: never passwords, codes, hashes or tokens.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import AppError
from app.models.enums import UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.user import User
from app.schemas.admin import UserAdminOut, UserAdminPage
from app.schemas.common import Pagination
from app.utils.search import LIKE_ESCAPE, contains_pattern, phone_prefix

logger = logging.getLogger(__name__)


def _role(user: Any) -> str:
    return str(getattr(user.role, "value", user.role))


def _is_super_admin(user: Any) -> bool:
    return bool(getattr(user, "is_super_admin", False))


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
