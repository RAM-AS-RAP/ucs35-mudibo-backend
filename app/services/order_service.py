"""Buyer checkout, order views, cancellation, and farmer order history.

Lock order used across this application: wherever both an order row and a listing row are locked
in one transaction, the order is locked first and the listing second. This matches
``public.expire_stale_orders()``, which updates orders before listings. Checkout locks only the
listing row (``SELECT ... FOR UPDATE``) and then inserts a new order row, which nobody else can
hold a lock on yet. Never lock a listing and then an existing order: that is the opposite order and
can deadlock against cancel or the expiry job.

The pay endpoint (``payment_service.initiate_payment``) locks the order row ``FOR UPDATE`` before
inserting a payment and checks that the order is ``pending_payment`` with enough time left. It does
not rely on the payment insert trigger for this: the trigger checks only the status (not
``expires_at``), and locking the order first is what serializes payment creation against a
concurrent cancel or expiry. The full lock order is orders, then payments, then listings.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.integrations.storage import public_url
from app.models.enums import ListingStatus, OrderStatus, PaymentStatus, UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.schemas.common import Pagination
from app.schemas.order import (
    PAYMENT_STATUS_MESSAGES,
    OrderListingSummary,
    OrderOut,
    OrderPage,
    OrderPaymentSummary,
)
from app.services import listing_service, payment_service
from app.utils.phone import mask_phone

REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REFERENCE_ATTEMPTS = 5
OPEN_PAYMENT_STATUSES = (PaymentStatus.INITIATING, PaymentStatus.INITIATED)


def calculate_total(quantity: Decimal, unit_price: Decimal) -> int:
    """Round a server-computed order amount up to the next whole shilling, in Decimal only."""
    return int((quantity * unit_price).quantize(Decimal("1"), rounding=ROUND_CEILING))


def generate_reference(length: int) -> str:
    """Return a random order reference without ambiguous characters (no I, L, O, 0, 1)."""
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(length))


def _is_reference_collision(exc: IntegrityError) -> bool:
    """Detect a unique violation on orders.reference without matching other constraints."""
    original = exc.orig
    cause = getattr(original, "__cause__", None)
    for candidate in (original, cause):
        if getattr(candidate, "constraint_name", None) == "orders_reference_key":
            return True
    return "orders_reference_key" in str(original)


async def create_order(session: AsyncSession, buyer: User, listing_id: UUID, quantity: Decimal) -> OrderOut:
    """Reserve stock and create a server-priced pending order in one transaction."""
    settings = get_settings()
    # The request session already has a transaction open (the auth dependency queried through it),
    # so session.begin() would raise. Everything below runs in that transaction and becomes durable
    # in the single commit at the end; any error before it rolls all of it back.
    # Lock order: checkout locks only this listing row, then inserts a new order (see module docstring).
    # A disabled farmer's listings are unavailable: the same 404 as an unknown listing.
    listing = await session.scalar(
        select(ProduceListing)
        .where(ProduceListing.id == listing_id, listing_service.farmer_not_disabled())
        .with_for_update(of=ProduceListing)
    )
    if listing is None or listing.status == ListingStatus.REMOVED:
        raise AppError(status.HTTP_404_NOT_FOUND, "listing_not_found", "Listing not found.")
    if listing.status != ListingStatus.ACTIVE:
        raise AppError(status.HTTP_409_CONFLICT, "listing_unavailable", "Listing is not available for ordering.")
    if listing.quantity_available < quantity:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "insufficient_stock",
            f"Only {listing.quantity_available} {listing.unit} available.",
        )

    pending = (
        await session.execute(
            select(Order.id, Order.listing_id).where(
                Order.buyer_id == buyer.id,
                Order.status == OrderStatus.PENDING_PAYMENT,
                Order.expires_at > func.now(),
            )
        )
    ).all()
    for existing_id, existing_listing_id in pending:
        if existing_listing_id == listing.id:
            raise AppError(
                status.HTTP_409_CONFLICT,
                "pending_order_exists",
                "You already have an order awaiting payment for this listing.",
                extra={"existing_order_id": str(existing_id)},
            )
    if len(pending) >= settings.max_pending_orders_per_buyer:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "too_many_pending_orders",
            "You have too many orders awaiting payment. Pay or cancel one first.",
        )

    unit_price = listing.price_per_unit
    total_amount = calculate_total(quantity, unit_price)
    if total_amount < settings.min_order_total_kes:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "order_too_small",
            f"Order total must be at least KES {settings.min_order_total_kes}.",
        )

    listing.quantity_available = listing.quantity_available - quantity
    listing.status = ListingStatus.SOLD_OUT if listing.quantity_available == 0 else ListingStatus.ACTIVE
    await session.flush()

    order_id = uuid4()
    for _ in range(REFERENCE_ATTEMPTS):
        try:
            async with session.begin_nested():
                session.add(
                    Order(
                        id=order_id,
                        reference=generate_reference(settings.order_reference_length),
                        listing_id=listing.id,
                        buyer_id=buyer.id,
                        buyer_role=UserRole.BUYER,
                        quantity=quantity,
                        unit_price=unit_price,
                        total_amount=total_amount,
                        status=OrderStatus.PENDING_PAYMENT,
                        # Database clock, never the application clock.
                        expires_at=func.now() + timedelta(minutes=settings.order_expiry_minutes),
                    )
                )
            break
        except IntegrityError as exc:
            if not _is_reference_collision(exc):
                raise
    else:
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "The order could not be created. Try again."
        )

    await session.commit()
    return await _fetch_order_view(session, order_id, buyer)


ORDER_VIEW_COLUMNS = """
    o.id,
    o.reference,
    o.buyer_id,
    o.status,
    o.quantity,
    o.unit_price,
    o.total_amount,
    o.expires_at,
    o.created_at,
    l.id as listing_id,
    l.crop,
    l.market,
    l.unit,
    l.image_path,
    l.status as listing_status,
    seller.name as seller_name,
    latest_payment.status as payment_status,
    latest_payment.mpesa_receipt_number,
    latest_payment.result_desc,
    latest_payment.stk_phone_number as payment_stk_phone_number,
    latest_payment.completed_at as payment_completed_at,
    payment_counts.attempts as payment_attempts,
    payment_counts.open_payments > 0 as has_open_payment,
    now() as server_time
"""

ORDER_VIEW_JOINS = """
    from public.orders as o
    join public.produce_listings as l on l.id = o.listing_id
    join public.users as seller on seller.id = l.farmer_id
    left join lateral (
        select p.status, p.mpesa_receipt_number, p.result_desc, p.stk_phone_number, p.completed_at
        from public.payments as p
        where p.order_id = o.id
        order by p.created_at desc, p.id desc
        limit 1
    ) as latest_payment on true
    cross join lateral (
        select
            count(*) as attempts,
            count(*) filter (where p.status in ('initiating', 'initiated')) as open_payments
        from public.payments as p
        where p.order_id = o.id
    ) as payment_counts
"""

# Composed once from the constant fragments above; values are always bound parameters.
ORDER_VIEW_BY_ID_SQL = (
    "select " + ORDER_VIEW_COLUMNS + " " + ORDER_VIEW_JOINS + " where o.id = cast(:order_id as uuid)"
)

MY_ORDERS_SQL = (
    """
            with filtered as (
                select """
    + ORDER_VIEW_COLUMNS
    + " "
    + ORDER_VIEW_JOINS
    + """
                where o.buyer_id = cast(:buyer_id as uuid)
                  and (cast(:status as public.order_status) is null or o.status = cast(:status as public.order_status))
            ), page as (
                select * from filtered order by created_at desc, id desc limit :limit offset :offset
            ), totals as (
                select count(*) as total from filtered
            )
            select page.*, totals.total
            from totals left join page on true
            order by page.created_at desc, page.id desc
        """
)


def _order_out(row: Any, viewer: User) -> OrderOut:
    """Build the order view, deriving expiry and permitted actions from database values."""
    settings = get_settings()
    order_status = str(row["status"])
    pending = order_status == OrderStatus.PENDING_PAYMENT
    is_expired = pending and row["expires_at"] <= row["server_time"]
    has_open_payment = bool(row["has_open_payment"])
    is_owner = str(getattr(viewer.role, "value", viewer.role)) == UserRole.BUYER and viewer.id == row["buyer_id"]
    enough_time_to_pay = row["expires_at"] - row["server_time"] >= timedelta(
        seconds=settings.mpesa_min_seconds_before_expiry
    )
    attempts_left = int(row["payment_attempts"] or 0) < settings.mpesa_max_attempts_per_order
    payment = None
    if row["payment_status"] is not None:
        payment_status = str(row["payment_status"])
        payment = OrderPaymentSummary(
            status=payment_status,
            status_message=PAYMENT_STATUS_MESSAGES.get(payment_status, payment_status),
            stk_target_masked=mask_phone(row.get("payment_stk_phone_number")),
            mpesa_receipt_number=row["mpesa_receipt_number"],
            result_desc=row["result_desc"],
            completed_at=row["payment_completed_at"],
            attempts=int(row["payment_attempts"]),
        )
    return OrderOut(
        id=row["id"],
        reference=row["reference"],
        status=order_status,
        is_expired=is_expired,
        quantity=row["quantity"],
        unit_price=row["unit_price"],
        total_amount=int(row["total_amount"]),
        expires_at=row["expires_at"],
        server_time=row["server_time"],
        created_at=row["created_at"],
        can_pay=is_owner
        and pending
        and not is_expired
        and enough_time_to_pay
        and not has_open_payment
        and attempts_left,
        can_cancel=is_owner and pending and not has_open_payment,
        listing=OrderListingSummary(
            id=row["listing_id"],
            crop=str(row["crop"]),
            market=row["market"],
            unit=row["unit"],
            image_url=public_url(row["image_path"]) if row["image_path"] else None,
            seller_name=row["seller_name"],
            status=str(row["listing_status"]),
        ),
        payment=payment,
    )


async def _fetch_order_view(session: AsyncSession, order_id: UUID, viewer: User) -> OrderOut:
    """Load one order with its listing, seller name, payment summary, and database time."""
    result = await session.execute(
        text(ORDER_VIEW_BY_ID_SQL),
        {"order_id": order_id},
    )
    row = result.mappings().first()
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found.")
    return _order_out(row, viewer)


async def list_my_orders(
    session: AsyncSession,
    buyer: User,
    pagination: Pagination,
    status_filter: OrderStatus | None = None,
) -> OrderPage:
    """Return the buyer's own orders, newest first, with summaries from one joined query."""
    result = await session.execute(
        text(MY_ORDERS_SQL),
        {
            "buyer_id": buyer.id,
            "status": status_filter.value if status_filter is not None else None,
            "limit": pagination.limit,
            "offset": pagination.offset,
        },
    )
    rows = result.mappings().all()
    return OrderPage(
        items=[_order_out(row, buyer) for row in rows if row["id"] is not None],
        total=int(rows[0]["total"]) if rows else 0,
        limit=pagination.limit,
        offset=pagination.offset,
    )


async def order_view(session: AsyncSession, order_id: UUID, viewer: User) -> OrderOut:
    """Return the order view without reconciling its payment (used by the pay endpoint)."""
    return await _fetch_order_view(session, order_id, viewer)


async def get_order(
    session: AsyncSession,
    order_id: UUID,
    viewer: User,
    *,
    session_factory: payment_service.SessionFactory | None = None,
    daraja_client: Any = None,
) -> OrderOut:
    """Return an order to its owning buyer or, read only, to an admin.

    For the owning buyer the latest payment is reconciled first. The read transaction is ended before
    that, so no transaction is open while reconciliation calls Daraja; admins never trigger it.
    """
    order = await session.scalar(select(Order).where(Order.id == order_id))
    is_admin = str(getattr(viewer.role, "value", viewer.role)) == UserRole.ADMIN
    if order is None or (not is_admin and order.buyer_id != viewer.id):
        raise AppError(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found.")
    if not is_admin and session_factory is not None:
        await session.commit()
        await payment_service.maybe_reconcile_payment(session_factory, order, viewer, daraja_client)
    return await _fetch_order_view(session, order.id, viewer)


async def cancel_order(session: AsyncSession, buyer: User, order_id: UUID) -> OrderOut:
    """Cancel an owned pending order and return its reserved stock, locking order then listing."""
    order = await session.scalar(
        select(Order).where(Order.id == order_id, Order.buyer_id == buyer.id).with_for_update()
    )
    if order is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found.")
    if order.status == OrderStatus.CANCELLED:
        return await _fetch_order_view(session, order.id, buyer)
    if order.status != OrderStatus.PENDING_PAYMENT:
        raise AppError(status.HTTP_409_CONFLICT, "order_not_cancellable", "This order can no longer be cancelled.")
    open_payment = await session.scalar(
        select(Payment.id).where(Payment.order_id == order.id, Payment.status.in_(OPEN_PAYMENT_STATUSES)).limit(1)
    )
    if open_payment is not None:
        raise AppError(status.HTTP_409_CONFLICT, "payment_in_progress", "A payment for this order is in progress.")

    # Lock order: the order row is already locked above; the listing is locked second, never first.
    listing = await session.scalar(
        select(ProduceListing).where(ProduceListing.id == order.listing_id).with_for_update()
    )
    order.status = OrderStatus.CANCELLED
    if listing is not None:
        listing.quantity_available = listing.quantity_available + order.quantity
        if listing.status == ListingStatus.SOLD_OUT:
            listing.status = ListingStatus.ACTIVE
    await session.commit()
    return await _fetch_order_view(session, order.id, buyer)


async def list_orders_for_farmer(
    session: AsyncSession,
    farmer: User,
    pagination: Any,
    status_filter: Any = None,
    listing_id: UUID | None = None,
) -> Any:
    """Return this farmer's listing orders with latest payment metadata in one query."""
    from decimal import Decimal

    from sqlalchemy import text

    from app.schemas.listing import FarmerOrderOut, FarmerOrdersPage

    result = await session.execute(
        text("""
            with filtered as (
                select
                    o.id as order_id,
                    o.reference,
                    o.listing_id,
                    l.crop,
                    l.market,
                    o.quantity,
                    o.unit_price,
                    o.total_amount,
                    o.status,
                    o.created_at,
                    buyer.name as buyer_name,
                    latest_payment.status as payment_status,
                    latest_payment.environment as payment_environment,
                    latest_payment.mpesa_receipt_number,
                    latest_payment.completed_at as payment_completed_at
                from public.orders as o
                join public.produce_listings as l on l.id = o.listing_id
                join public.users as buyer on buyer.id = o.buyer_id
                left join lateral (
                    select p.status, p.environment, p.mpesa_receipt_number, p.completed_at
                    from public.payments as p
                    where p.order_id = o.id
                    order by p.created_at desc
                    limit 1
                ) as latest_payment on true
                where l.farmer_id = :farmer_id
                  and (cast(:status as public.order_status) is null or o.status = cast(:status as public.order_status))
                  and (cast(:listing_id as uuid) is null or o.listing_id = cast(:listing_id as uuid))
            ), page as (
                select * from filtered order by created_at desc limit :limit offset :offset
            ), totals as (
                select count(*) as total from filtered
            )
            select page.*, totals.total
            from totals left join page on true
            order by page.created_at desc
        """),
        {
            "farmer_id": farmer.id,
            "status": status_filter.value if status_filter is not None else None,
            "listing_id": listing_id,
            "limit": pagination.limit,
            "offset": pagination.offset,
        },
    )
    rows = result.mappings().all()
    total = int(rows[0]["total"]) if rows else 0
    items = [
        FarmerOrderOut(
            order_id=row["order_id"],
            reference=row["reference"],
            listing_id=row["listing_id"],
            crop=str(row["crop"]),
            market=row["market"],
            quantity=row["quantity"],
            unit_price=row["unit_price"],
            total_amount=Decimal(row["total_amount"]),
            status=str(row["status"]),
            created_at=row["created_at"],
            buyer_name=row["buyer_name"],
            payment_status=str(row["payment_status"]) if row["payment_status"] is not None else None,
            payment_environment=row["payment_environment"],
            mpesa_receipt_number=row["mpesa_receipt_number"],
            payment_completed_at=row["payment_completed_at"],
        )
        for row in rows
        if row["order_id"] is not None
    ]
    return FarmerOrdersPage(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        note="A paid status is a simulated sandbox payment confirmation; delivery is arranged outside the platform.",
    )
