"""Farmer-owned listing and order-history services."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from fastapi import UploadFile, status
from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.errors import AppError
from app.integrations.storage import delete_image, public_url, upload_image
from app.models.enums import CropType, ListingStatus, OrderStatus, UserRole
from app.models.listing import ProduceListing
from app.models.market import MarketLocation
from app.models.order import Order
from app.models.user import User
from app.schemas.common import Pagination
from app.schemas.listing import (
    ListingBrowseFilters,
    ListingOut,
    ListingPage,
    ListingSort,
    PublicListingDetail,
    PublicListingOut,
    PublicListingPage,
)
from app.services.market_service import require_available_market

ALLOWED_LISTING_UNITS = frozenset({"kg"})
MAX_LISTING_VALUE = Decimal("100000")
FARMER_ORDER_NOTE = (
    "A paid status is a simulated sandbox payment confirmation; delivery is arranged outside the platform."
)


def parse_decimal_input(raw: object, field: str, *, allow_zero: bool = False) -> Decimal:
    """Parse finite decimal input with at most two places and bounded magnitude."""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"{field} must be a decimal number."
        ) from exc
    if not value.is_finite():
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"{field} must be finite.")
    if max(0, -value.as_tuple().exponent) > 2:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"{field} may have at most two decimal places."
        )
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"{field} must be {qualifier}.")
    if value > MAX_LISTING_VALUE:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"{field} must not exceed 100000.")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _listing_out(listing: ProduceListing) -> ListingOut:
    return ListingOut(
        id=listing.id,
        farmer_id=listing.farmer_id,
        crop=listing.crop.value,
        market=listing.market,
        quantity_available=listing.quantity_available,
        unit=listing.unit,
        price_per_unit=listing.price_per_unit,
        image_url=public_url(listing.image_path) if listing.image_path else None,
        status=listing.status.value,
        created_at=listing.created_at,
        updated_at=listing.updated_at,
    )


async def _validate_listing_values(
    session: AsyncSession, values: dict[str, Any], *, allow_zero: bool
) -> dict[str, Any]:
    try:
        crop = CropType(str(values["crop"]).strip().lower())
    except (KeyError, ValueError) as exc:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Crop must be maize or beans.",
        ) from exc
    market = await require_available_market(session, str(values.get("market", "")))
    unit = str(values.get("unit", "kg")).strip().lower()
    if unit not in ALLOWED_LISTING_UNITS:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "Unit must be kg.")
    quantity = parse_decimal_input(values.get("quantity_available"), "quantity_available", allow_zero=allow_zero)
    price = parse_decimal_input(values.get("price_per_unit"), "price_per_unit")
    return {"crop": crop, "market": market, "unit": unit, "quantity_available": quantity, "price_per_unit": price}


async def create_listing(
    session: AsyncSession, farmer: User, values: dict[str, Any], image: UploadFile | None = None
) -> ListingOut:
    """Validate and create a farmer listing, compensating an uploaded object on DB failure."""
    from app.utils.images import process_image

    settings = get_settings()
    active_count = await session.scalar(
        select(func.count())
        .select_from(ProduceListing)
        .where(
            ProduceListing.farmer_id == farmer.id,
            ProduceListing.status == ListingStatus.ACTIVE,
        )
    )
    if int(active_count or 0) >= settings.max_active_listings_per_farmer:
        raise AppError(status.HTTP_409_CONFLICT, "listing_limit_reached", "Active listing limit reached.")

    validated = await _validate_listing_values(session, values, allow_zero=False)
    listing_id = uuid4()
    image_path: str | None = None
    if image is not None:
        image_bytes = await process_image(image)
        image_path = f"listings/{listing_id}/{uuid4()}.jpg"
        await upload_image(image_path, image_bytes)

    try:
        listing = ProduceListing(
            id=listing_id,
            farmer_id=farmer.id,
            farmer_role=UserRole.FARMER,
            crop=validated["crop"],
            market=validated["market"],
            quantity_available=validated["quantity_available"],
            unit=validated["unit"],
            price_per_unit=validated["price_per_unit"],
            image_path=image_path,
            status=ListingStatus.ACTIVE,
        )
        session.add(listing)
        await session.commit()
    except Exception:
        await session.rollback()
        if image_path is not None:
            await delete_image(image_path)
        raise
    await session.refresh(listing)
    return _listing_out(listing)


def _public_listing_values(listing: ProduceListing, seller_name: str) -> dict[str, Any]:
    return {
        "id": listing.id,
        "crop": listing.crop.value,
        "market": listing.market,
        "quantity_available": listing.quantity_available,
        "unit": listing.unit,
        "price_per_unit": listing.price_per_unit,
        "image_url": public_url(listing.image_path) if listing.image_path else None,
        "seller_name": seller_name,
        "status": listing.status.value,
        "created_at": listing.created_at,
    }


async def _require_market_location(session: AsyncSession, market: str) -> str:
    """Return the canonical market name from market_locations, or reject the filter with 422."""
    canonical = await session.scalar(
        select(MarketLocation.market_name).where(func.lower(MarketLocation.market_name) == market.strip().lower())
    )
    if canonical is None:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "Market is not a known market.")
    return str(canonical)


def farmer_not_disabled() -> ColumnElement[bool]:
    """Condition hiding listings whose farmer is disabled; re-enabling the farmer shows them again."""
    disabled_farmer = aliased(User)
    return ~exists().where(disabled_farmer.id == ProduceListing.farmer_id, disabled_farmer.is_disabled.is_(True))


def buyer_visible_listings_clause() -> ColumnElement[bool]:
    """The one rule for listings a buyer can see and buy: active, in stock, farmer not disabled.

    Used by the browse query and the buyer markets list. ``listing_is_buyer_visible`` is the same rule
    for a row that is already loaded (listing detail ``available`` and checkout); keep the two together.
    """
    return and_(
        ProduceListing.status == ListingStatus.ACTIVE,
        ProduceListing.quantity_available > 0,
        farmer_not_disabled(),
    )


def listing_is_buyer_visible(listing: ProduceListing, *, farmer_disabled: bool) -> bool:
    """``buyer_visible_listings_clause`` evaluated on a loaded listing and its farmer's disabled flag."""
    return listing.status == ListingStatus.ACTIVE and listing.quantity_available > 0 and not farmer_disabled


_BROWSE_ORDER = {
    ListingSort.NEWEST: (ProduceListing.created_at.desc(), ProduceListing.id.asc()),
    ListingSort.PRICE_ASC: (ProduceListing.price_per_unit.asc(), ProduceListing.id.asc()),
    ListingSort.PRICE_DESC: (ProduceListing.price_per_unit.desc(), ProduceListing.id.asc()),
}


async def browse_listings(
    session: AsyncSession, filters: ListingBrowseFilters, pagination: Pagination
) -> PublicListingPage:
    """Return active listings with stock for buyers, using one count query and one joined page query."""
    conditions = [buyer_visible_listings_clause()]
    if filters.market is not None:
        conditions.append(ProduceListing.market == await _require_market_location(session, filters.market))
    if filters.crop is not None:
        conditions.append(ProduceListing.crop == filters.crop)
    if filters.min_price is not None:
        conditions.append(ProduceListing.price_per_unit >= filters.min_price)
    if filters.max_price is not None:
        conditions.append(ProduceListing.price_per_unit <= filters.max_price)

    total = int(await session.scalar(select(func.count()).select_from(ProduceListing).where(*conditions)) or 0)
    result = await session.execute(
        select(ProduceListing, User.name)
        .join(User, User.id == ProduceListing.farmer_id)
        .where(*conditions)
        .order_by(*_BROWSE_ORDER[filters.sort])
        .limit(pagination.limit)
        .offset(pagination.offset)
    )
    return PublicListingPage(
        items=[PublicListingOut(**_public_listing_values(listing, name)) for listing, name in result.all()],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


async def get_public_listing(session: AsyncSession, listing_id: UUID) -> PublicListingDetail:
    """Return an active or sold-out listing for buyers; removed, unknown and disabled-farmer ids are the same 404."""
    result = await session.execute(
        select(ProduceListing, User.name)
        .join(User, User.id == ProduceListing.farmer_id)
        .where(
            ProduceListing.id == listing_id,
            ProduceListing.status.in_([ListingStatus.ACTIVE, ListingStatus.SOLD_OUT]),
            User.is_disabled.is_(False),
        )
    )
    row = result.first()
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "listing_not_found", "Listing not found.")
    listing, seller_name = row
    # The query above already excluded disabled farmers.
    available = listing_is_buyer_visible(listing, farmer_disabled=False)
    return PublicListingDetail(**_public_listing_values(listing, seller_name), available=available)


async def my_listings(
    session: AsyncSession,
    farmer: User,
    pagination: Pagination,
    status_filter: ListingStatus | None = None,
) -> ListingPage:
    """Return this farmer's listings, newest first, including removed rows by default."""
    conditions = [ProduceListing.farmer_id == farmer.id]
    if status_filter is not None:
        conditions.append(ProduceListing.status == status_filter)
    total = int(await session.scalar(select(func.count()).select_from(ProduceListing).where(*conditions)) or 0)
    rows = await session.scalars(
        select(ProduceListing)
        .where(*conditions)
        .order_by(ProduceListing.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    )
    return ListingPage(
        items=[_listing_out(listing) for listing in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


async def update_listing(
    session: AsyncSession,
    farmer: User,
    listing_id: UUID,
    values: dict[str, Any],
    image: UploadFile | None = None,
) -> ListingOut:
    """Lock and update an owned listing; replace/delete old storage only after commit."""
    from app.utils.images import process_image

    quantity_raw = values.get("quantity_available")
    price_raw = values.get("price_per_unit")
    remove_image = bool(values.get("remove_image", False))
    if quantity_raw is None and price_raw is None and image is None and not remove_image:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "no_changes", "No listing changes were provided.")

    listing = await session.scalar(
        select(ProduceListing)
        .where(ProduceListing.id == listing_id, ProduceListing.farmer_id == farmer.id)
        .with_for_update()
    )
    if listing is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "listing_not_found", "Listing not found.")
    if listing.status == ListingStatus.REMOVED:
        raise AppError(status.HTTP_409_CONFLICT, "listing_removed", "Removed listings cannot be edited.")

    if quantity_raw is not None:
        quantity = parse_decimal_input(quantity_raw, "quantity_available", allow_zero=True)
        listing.quantity_available = quantity
        if quantity == 0:
            listing.status = ListingStatus.SOLD_OUT
        elif listing.status == ListingStatus.SOLD_OUT:
            listing.status = ListingStatus.ACTIVE
    if price_raw is not None:
        listing.price_per_unit = parse_decimal_input(price_raw, "price_per_unit")

    old_image_path = listing.image_path
    new_image_path: str | None = None
    if image is not None:
        image_bytes = await process_image(image)
        new_image_path = f"listings/{listing.id}/{uuid4()}.jpg"
        await upload_image(new_image_path, image_bytes)
        listing.image_path = new_image_path
    elif remove_image:
        listing.image_path = None

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        if new_image_path is not None:
            await delete_image(new_image_path)
        raise
    await session.refresh(listing)

    if old_image_path and old_image_path != listing.image_path:
        await delete_image(old_image_path)
    return _listing_out(listing)


async def remove_listing(session: AsyncSession, actor: User, listing_id: UUID) -> None:
    """Soft-remove an owned listing unless a pending-payment order still reserves stock."""
    listing = await session.scalar(
        select(ProduceListing)
        .where(ProduceListing.id == listing_id, ProduceListing.farmer_id == actor.id)
        .with_for_update()
    )
    if listing is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "listing_not_found", "Listing not found.")
    if listing.status == ListingStatus.REMOVED:
        return None
    pending_order_id = await session.scalar(
        select(Order.id).where(Order.listing_id == listing.id, Order.status == OrderStatus.PENDING_PAYMENT).limit(1)
    )
    if pending_order_id is not None:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "listing_has_pending_orders",
            "Listing has orders awaiting payment.",
        )
    listing.status = ListingStatus.REMOVED
    listing.removed_at = datetime.now(UTC)
    listing.removed_by = actor.id
    await session.commit()
    return None
