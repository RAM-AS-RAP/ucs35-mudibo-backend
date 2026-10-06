"""Administrator service boundaries."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


async def create_admin(session: AsyncSession, actor: Any, values: dict[str, Any]) -> Any:
    """TODO: create a non-super admin with a forced password change."""
    # TODO: enforce created_by and temporary-password rules after migrations are applied.
    raise NotImplementedError


async def users(session: AsyncSession, **filters: Any) -> Any:
    """TODO: filter and paginate user accounts."""
    # TODO: apply role filters and pagination against the users table.
    raise NotImplementedError


async def set_user_disabled(session: AsyncSession, actor: Any, user_id: UUID, disabled: bool) -> Any:
    """TODO: enforce admin disable rules and set audit attributes."""
    # TODO: apply super-admin/self-disable checks and persist the audit fields.
    raise NotImplementedError


async def listings(session: AsyncSession, **filters: Any) -> Any:
    """TODO: return paginated administrative listings."""
    # TODO: query and paginate produce listings.
    raise NotImplementedError


async def payments(session: AsyncSession, **filters: Any) -> Any:
    """TODO: return payments with order references."""
    # TODO: join payment rows to order references and paginate.
    raise NotImplementedError


async def stats(session: AsyncSession) -> Any:
    """TODO: aggregate user, listing, order, and sandbox payment counts."""
    # TODO: compute grouped counts and completed sandbox totals.
    raise NotImplementedError


async def forecast_accuracy(session: AsyncSession, **filters: Any) -> Any:
    """TODO: query forecast_accuracy view and compute pooled summary."""
    # TODO: query filtered view rows and calculate non-null pooled metrics.
    raise NotImplementedError
