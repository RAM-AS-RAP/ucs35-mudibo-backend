"""Order creation and total calculation."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.order import Order
    from app.models.user import User


def calculate_total(quantity: Decimal, unit_price: Decimal) -> int:
    """Round a server-computed order amount up to the next whole shilling."""
    return int((quantity * unit_price).quantize(Decimal("1"), rounding=ROUND_CEILING))


async def create_order(session: AsyncSession, buyer: User, listing_id: UUID, quantity: Decimal) -> Order:
    """Lock stock, decrement it, and persist a server-priced order atomically."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.models.enums import ListingStatus, OrderStatus, UserRole
    from app.models.listing import ProduceListing
    from app.models.order import Order

    settings = get_settings()
    async with session.begin():
        listing = await session.scalar(select(ProduceListing).where(ProduceListing.id == listing_id).with_for_update())
        if listing is None or listing.status != ListingStatus.ACTIVE or listing.quantity_available < quantity:
            raise HTTPException(
                status_code=409,
                detail={"code": "stock_unavailable", "message": "Listing is unavailable or has insufficient stock"},
            )
        listing.quantity_available -= quantity
        if listing.quantity_available == 0:
            listing.status = ListingStatus.SOLD_OUT
        reference = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(8))
        order = Order(
            reference=reference,
            listing_id=listing.id,
            buyer_id=buyer.id,
            buyer_role=UserRole.BUYER,
            quantity=quantity,
            unit_price=listing.price_per_unit,
            total_amount=calculate_total(quantity, listing.price_per_unit),
            status=OrderStatus.PENDING_PAYMENT,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.order_expiry_minutes),
        )
        session.add(order)
        await session.flush()
    await session.refresh(order)
    return order


async def get_order(session: AsyncSession, order_id: UUID, user: User) -> dict[str, Any]:
    """Retrieve an order and reconcile its stale latest payment on demand."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.integrations.daraja import DarajaClient
    from app.models.enums import PaymentStatus, UserRole
    from app.models.order import Order
    from app.models.payment import Payment
    from app.schemas.order import OrderRead
    from app.schemas.payment import PaymentRead
    from app.services.payment_service import reconcile_payment

    order = await session.get(Order, order_id)
    if order is None or (user.role != UserRole.ADMIN and order.buyer_id != user.id):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Order not found"})
    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()).limit(1)
    )
    settings = get_settings()
    if (
        payment is not None
        and payment.status == PaymentStatus.INITIATED
        and (datetime.now(UTC) - payment.created_at).total_seconds() >= settings.reconcile_after_seconds
    ):
        await reconcile_payment(session, payment, DarajaClient(settings))
        await session.refresh(order)
        await session.refresh(payment)
    return {
        "order": OrderRead.model_validate(order),
        "latest_payment": PaymentRead.model_validate(payment) if payment is not None else None,
    }


async def list_my_orders(session: AsyncSession, buyer: User) -> list[Any]:
    """Return the buyer's orders newest first."""
    from sqlalchemy import select

    from app.models.order import Order
    from app.schemas.order import OrderRead

    result = await session.scalars(select(Order).where(Order.buyer_id == buyer.id).order_by(Order.created_at.desc()))
    return [OrderRead.model_validate(order) for order in result]
