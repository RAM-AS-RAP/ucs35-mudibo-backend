"""Administrator listing moderation: review listings and soft-remove them.

Lock order, shared with the rest of the application: orders, then payments, then listings.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import AppError
from app.integrations.storage import public_url
from app.models.enums import CropType, ListingStatus, OrderStatus, PaymentStatus
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.schemas.admin import AdminListingOut, AdminListingPage
from app.schemas.common import Pagination
from app.utils.search import LIKE_ESCAPE, contains_pattern

logger = logging.getLogger(__name__)

OPEN_PAYMENT_STATUSES = (PaymentStatus.INITIATING, PaymentStatus.INITIATED)


async def list_listings(
    session: AsyncSession,
    pagination: Pagination,
    *,
    status_filter: ListingStatus | None = None,
    crop: CropType | None = None,
    market: str | None = None,
    farmer_id: UUID | None = None,
    q: str | None = None,
) -> AdminListingPage:
    """All listings newest first, with order counts from a grouped subquery: one count and one page statement."""
    farmer = aliased(User)
    remover = aliased(User)
    order_counts = (
        select(
            Order.listing_id.label("listing_id"),
            func.count().filter(Order.status == OrderStatus.PENDING_PAYMENT).label("pending"),
            func.count().filter(Order.status == OrderStatus.PAID).label("paid"),
        )
        .group_by(Order.listing_id)
        .subquery()
    )
    conditions: list[Any] = []
    if status_filter is not None:
        conditions.append(ProduceListing.status == status_filter)
    if crop is not None:
        conditions.append(ProduceListing.crop == crop)
    if market is not None:
        conditions.append(func.lower(ProduceListing.market) == market.strip().lower())
    if farmer_id is not None:
        conditions.append(ProduceListing.farmer_id == farmer_id)
    if q is not None:
        conditions.append(farmer.name.ilike(contains_pattern(q), escape=LIKE_ESCAPE))

    total = int(
        await session.scalar(
            select(func.count())
            .select_from(ProduceListing)
            .join(farmer, farmer.id == ProduceListing.farmer_id)
            .where(*conditions)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(
                ProduceListing,
                farmer.name,
                farmer.is_disabled,
                remover.name,
                order_counts.c.pending,
                order_counts.c.paid,
            )
            .join(farmer, farmer.id == ProduceListing.farmer_id)
            .outerjoin(remover, remover.id == ProduceListing.removed_by)
            .outerjoin(order_counts, order_counts.c.listing_id == ProduceListing.id)
            .where(*conditions)
            .order_by(ProduceListing.created_at.desc(), ProduceListing.id.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()
    items = [
        AdminListingOut(
            id=listing.id,
            farmer_id=listing.farmer_id,
            farmer_name=farmer_name,
            farmer_disabled=bool(farmer_disabled),
            crop=str(listing.crop),
            market=listing.market,
            quantity_available=listing.quantity_available,
            unit=listing.unit,
            price_per_unit=listing.price_per_unit,
            image_url=public_url(listing.image_path) if listing.image_path else None,
            status=str(listing.status),
            removed_at=listing.removed_at,
            removed_by_name=removed_by_name,
            pending_orders=int(pending or 0),
            paid_orders=int(paid or 0),
            created_at=listing.created_at,
        )
        for listing, farmer_name, farmer_disabled, removed_by_name, pending, paid in rows
    ]
    return AdminListingPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


async def remove_listing(session: AsyncSession, actor: User, listing_id: UUID) -> None:
    """Soft-remove a listing, cancelling its unpaid orders; refused while one of them has an open payment.

    One transaction: lock the pending orders (by id), check their payments, then lock the listing.
    Each cancelled order's quantity is added back before removal, so stock totals stay consistent.
    """
    orders = list(
        (
            await session.scalars(
                select(Order)
                .where(Order.listing_id == listing_id, Order.status == OrderStatus.PENDING_PAYMENT)
                .order_by(Order.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if orders:
        open_payment = await session.scalar(
            select(Payment.id)
            .where(Payment.order_id.in_([order.id for order in orders]), Payment.status.in_(OPEN_PAYMENT_STATUSES))
            .limit(1)
            .with_for_update()
        )
        if open_payment is not None:
            raise AppError(
                status.HTTP_409_CONFLICT,
                "listing_has_open_payments",
                "A buyer is paying for an order on this listing. Try again in a few minutes, "
                "after the payment finishes or the order expires.",
            )

    listing = await session.scalar(
        select(ProduceListing)
        .where(ProduceListing.id == listing_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if listing is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "listing_not_found", "Listing not found.")
    if listing.status == ListingStatus.REMOVED:
        await session.commit()  # already removed: release the locks and change nothing
        return

    restored = Decimal("0")
    for order in orders:
        order.status = OrderStatus.CANCELLED
        restored += order.quantity
    listing.quantity_available = listing.quantity_available + restored
    listing.status = ListingStatus.REMOVED
    listing.removed_at = func.now()
    listing.removed_by = actor.id
    await session.commit()
    logger.info("listing_removed actor=%s target=%s", actor.id, listing_id)
