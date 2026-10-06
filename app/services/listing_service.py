"""Marketplace listing service boundaries."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


async def create_listing(session: AsyncSession, farmer: Any, values: dict[str, Any], image: Any = None) -> Any:
    """TODO: validate/upload image, insert listing, and clean up failed uploads."""
    # TODO: implement image signature/size validation and compensating storage delete.
    raise NotImplementedError


async def list_listings(session: AsyncSession, **filters: Any) -> list[Any]:
    """TODO: list active in-stock listings with filters and pagination."""
    # TODO: filter active inventory and serialize listing images.
    raise NotImplementedError


async def get_listing(session: AsyncSession, listing_id: UUID) -> Any:
    """TODO: return one listing or raise a not-found API error."""
    # TODO: query the listing by identifier.
    raise NotImplementedError


async def my_listings(session: AsyncSession, farmer: Any) -> list[Any]:
    """TODO: list the farmer's listings."""
    # TODO: filter listings by farmer id.
    raise NotImplementedError


async def update_listing(
    session: AsyncSession, farmer: Any, listing_id: UUID, values: dict[str, Any], image: Any = None
) -> Any:
    """TODO: lock listing row and apply owner-only changes."""
    # TODO: lock stock during edits and replace images with cleanup on failure.
    raise NotImplementedError


async def remove_listing(session: AsyncSession, actor: Any, listing_id: UUID) -> None:
    """TODO: soft-remove listing, refusing removal with pending orders."""
    # TODO: check pending orders and record removed_at/removed_by.
    raise NotImplementedError


async def farmer_orders(session: AsyncSession, farmer: Any) -> list[Any]:
    """TODO: list orders placed against this farmer's listings."""
    # TODO: join orders through listings and include buyer display information.
    raise NotImplementedError
