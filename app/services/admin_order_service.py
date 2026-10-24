"""Administrator review of orders and payments, and resolution of ``needs_reconciliation`` orders.

Lock order, shared with the rest of the application: orders, then payments, then listings.
Responses never contain idempotency keys, merchant request ids, or full payment phone numbers.
Resolution never moves money: a voided sale's refund is handled outside the platform.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.enums import ListingStatus, OrderStatus, PaymentStatus
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.payment import Payment, PaymentEvent
from app.models.user import User
from app.schemas.admin import (
    AdminOrderDetail,
    AdminOrderOut,
    AdminOrderPage,
    AdminOrderPaymentOut,
    AdminPaymentOut,
    AdminPaymentPage,
    PaymentEventList,
    PaymentEventOut,
    ReconciliationOut,
    ResolutionOut,
    ResolveAction,
    ResolveOrderRequest,
)
from app.schemas.common import Pagination
from app.utils.phone import mask_phone, mask_phone_or_none
from app.utils.search import contains_pattern, prefix_pattern

logger = logging.getLogger(__name__)

EVENTS_LIMIT = 100
RESERVED_BEFORE = "pending_payment"
RESTORED_BEFORE = ("expired", "cancelled")

# The latest error event of the order's payments that records order_status_before. `->>` is used rather
# than the `?` operator, which would clash with bound parameters.
RECONCILIATION_CONTEXT_TEMPLATE = """
    select e.payload ->> 'reason' as reason,
           e.payload ->> 'order_status_before' as order_status_before,
           e.created_at
    from public.payment_events as e
    join public.payments as p on p.id = e.payment_id
    where p.order_id = {order_id}
      and e.event_type = 'error'
      and (e.payload ->> 'order_status_before') is not null
    order by e.created_at desc, e.id desc
    limit 1
"""
RECONCILIATION_CONTEXT_SQL = RECONCILIATION_CONTEXT_TEMPLATE.format(order_id="o.id")
RECONCILIATION_CONTEXT_FOR_ORDER_SQL = RECONCILIATION_CONTEXT_TEMPLATE.format(order_id="cast(:order_id as uuid)")

ORDER_SELECT = f"""
    select
        o.id,
        o.reference,
        o.status::text as status,
        buyer.name as buyer_name,
        seller.name as seller_name,
        o.listing_id,
        l.crop::text as crop,
        l.market,
        o.quantity,
        o.unit_price,
        o.total_amount,
        o.created_at,
        o.expires_at,
        lp.status::text as latest_payment_status,
        lp.mpesa_receipt_number as latest_mpesa_receipt_number,
        rc.reason as rc_reason,
        rc.order_status_before as rc_order_status_before,
        rc.created_at as rc_flagged_at,
        rs.action as rs_action,
        rs.note as rs_note,
        rs.admin_name as rs_admin_name,
        rs.created_at as rs_created_at
    from public.orders as o
    join public.users as buyer on buyer.id = o.buyer_id
    join public.produce_listings as l on l.id = o.listing_id
    join public.users as seller on seller.id = l.farmer_id
    left join lateral (
        select p.status, p.mpesa_receipt_number
        from public.payments as p
        where p.order_id = o.id
        order by p.created_at desc, p.id desc
        limit 1
    ) as lp on true
    left join lateral ({RECONCILIATION_CONTEXT_SQL}) as rc on o.status = 'needs_reconciliation'
    left join lateral (
        select e.payload ->> 'action' as action,
               e.payload ->> 'note' as note,
               admin_user.name as admin_name,
               e.created_at
        from public.payment_events as e
        join public.payments as p on p.id = e.payment_id
        left join public.users as admin_user on admin_user.id::text = (e.payload ->> 'admin_id')
        where p.order_id = o.id and e.event_type = 'admin_action'
        order by e.created_at desc, e.id desc
        limit 1
    ) as rs on true
"""

ORDER_COUNT = """
    select count(*)
    from public.orders as o
    join public.users as buyer on buyer.id = o.buyer_id
"""


def _order_filters(status_filter: OrderStatus | None, q: str | None) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if status_filter is not None:
        clauses.append("o.status = cast(:status as public.order_status)")
        params["status"] = status_filter.value
    if q is not None:
        clauses.append(r"(o.reference ilike :ref_prefix escape '\' or buyer.name ilike :buyer_name escape '\')")
        params["ref_prefix"] = prefix_pattern(q.upper())
        params["buyer_name"] = contains_pattern(q)
    return (" where " + " and ".join(clauses)) if clauses else "", params


def _order_out(row: Any, model: type[AdminOrderOut] = AdminOrderOut, **extra: Any) -> Any:
    reconciliation = None
    if row["status"] == OrderStatus.NEEDS_RECONCILIATION and row["rc_order_status_before"] is not None:
        reconciliation = ReconciliationOut(
            reason=row["rc_reason"],
            order_status_before=row["rc_order_status_before"],
            flagged_at=row["rc_flagged_at"],
        )
    resolution = None
    if row["rs_created_at"] is not None:
        resolution = ResolutionOut(
            action=row["rs_action"],
            note=row["rs_note"],
            admin_name=row["rs_admin_name"],
            created_at=row["rs_created_at"],
        )
    return model(
        id=row["id"],
        reference=row["reference"],
        status=row["status"],
        buyer_name=row["buyer_name"],
        seller_name=row["seller_name"],
        listing_id=row["listing_id"],
        crop=row["crop"],
        market=row["market"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
        total_amount=int(row["total_amount"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        latest_payment_status=row["latest_payment_status"],
        latest_mpesa_receipt_number=row["latest_mpesa_receipt_number"],
        reconciliation=reconciliation,
        resolution=resolution,
        **extra,
    )


async def list_orders(
    session: AsyncSession,
    pagination: Pagination,
    *,
    status_filter: OrderStatus | None = None,
    q: str | None = None,
) -> AdminOrderPage:
    """Orders newest first with payment, reconciliation and resolution summaries: one count and one page query."""
    where, params = _order_filters(status_filter, q)
    total = int(await session.scalar(text(ORDER_COUNT + where), params) or 0)
    rows = (
        (
            await session.execute(
                text(ORDER_SELECT + where + " order by o.created_at desc, o.id desc limit :limit offset :offset"),
                {**params, "limit": pagination.limit, "offset": pagination.offset},
            )
        )
        .mappings()
        .all()
    )
    return AdminOrderPage(
        items=[_order_out(row) for row in rows], total=total, limit=pagination.limit, offset=pagination.offset
    )


def _payment_out(payment: Payment) -> AdminOrderPaymentOut:
    return AdminOrderPaymentOut(
        id=payment.id,
        status=str(payment.status),
        amount=payment.amount,
        phone_masked=mask_phone_or_none(payment.phone_number),
        stk_target_masked=mask_phone(payment.stk_phone_number),
        mpesa_receipt_number=payment.mpesa_receipt_number,
        result_code=payment.result_code,
        result_desc=payment.result_desc,
        environment=payment.environment,
        created_at=payment.created_at,
        completed_at=payment.completed_at,
        callback_received_at=payment.callback_received_at,
    )


async def get_order(session: AsyncSession, order_id: UUID) -> AdminOrderDetail:
    """One order with all its payments (oldest first) and the latest resolution, if any."""
    row = (
        (await session.execute(text(ORDER_SELECT + " where o.id = cast(:order_id as uuid)"), {"order_id": order_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found.")
    payments = (
        await session.scalars(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.created_at.asc(), Payment.id.asc())
            .execution_options(populate_existing=True)
        )
    ).all()
    return _order_out(row, AdminOrderDetail, payments=[_payment_out(payment) for payment in payments])


async def _reconciliation_context(session: AsyncSession, order_id: UUID) -> tuple[str | None, str] | None:
    row = (await session.execute(text(RECONCILIATION_CONTEXT_FOR_ORDER_SQL), {"order_id": order_id})).first()
    if row is None or row[1] is None:
        return None
    return row[0], str(row[1])


async def resolve_order(
    session: AsyncSession, actor: User, order_id: UUID, body: ResolveOrderRequest
) -> AdminOrderDetail:
    """Resolve a ``needs_reconciliation`` order as a confirmed sale or a voided order, in one transaction.

    Locks the order, then its payments, then the listing. Stock is derived from ``order_status_before``:
    ``pending_payment`` means the order's quantity is still reserved, ``expired`` or ``cancelled`` means it
    was already returned to the listing. A removed listing is never touched. Payment rows are left as they
    are; a voided sale's refund happens outside the platform.
    """
    order = await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    if order is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found.")
    if order.status != OrderStatus.NEEDS_RECONCILIATION:
        raise AppError(
            status.HTTP_409_CONFLICT, "order_not_in_reconciliation", "This order is not awaiting reconciliation."
        )
    context = await _reconciliation_context(session, order.id)
    if context is None or (context[1] != RESERVED_BEFORE and context[1] not in RESTORED_BEFORE):
        raise AppError(
            status.HTTP_409_CONFLICT,
            "reconciliation_context_missing",
            "The reason this order was flagged could not be found.",
        )
    _, order_status_before = context

    payments = list(
        (
            await session.scalars(
                select(Payment)
                .where(Payment.order_id == order.id)
                .order_by(Payment.created_at.asc(), Payment.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    listing = await session.scalar(
        select(ProduceListing)
        .where(ProduceListing.id == order.listing_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    stock_reserved = order_status_before == RESERVED_BEFORE
    listing_usable = listing is not None and listing.status != ListingStatus.REMOVED
    stock_change = Decimal("0")
    previous_status = str(order.status)

    if body.action == ResolveAction.CONFIRM_SALE:
        if listing_usable and not stock_reserved:
            assert listing is not None
            if listing.quantity_available < order.quantity:
                raise AppError(
                    status.HTTP_409_CONFLICT,
                    "insufficient_stock_to_confirm",
                    f"Only {listing.quantity_available} {listing.unit} left; the sale cannot be re-reserved.",
                )
            listing.quantity_available = listing.quantity_available - order.quantity
            if listing.quantity_available == 0:
                listing.status = ListingStatus.SOLD_OUT
            stock_change = -order.quantity
        order.status = OrderStatus.PAID
    else:
        if listing_usable and stock_reserved:
            assert listing is not None
            listing.quantity_available = listing.quantity_available + order.quantity
            if listing.status == ListingStatus.SOLD_OUT:
                listing.status = ListingStatus.ACTIVE
            stock_change = order.quantity
        order.status = OrderStatus.CANCELLED

    latest_payment = payments[-1] if payments else None
    session.add(
        PaymentEvent(
            id=uuid4(),
            payment_id=latest_payment.id if latest_payment is not None else None,
            checkout_request_id=latest_payment.checkout_request_id if latest_payment is not None else None,
            event_type="admin_action",
            payload={
                "action": body.action.value,
                "note": body.note,
                "admin_id": str(actor.id),
                "order_id": str(order.id),
                "previous_status": previous_status,
                "order_status_before": order_status_before,
                # Signed quantity change applied to the listing, as a decimal string ("0" when none).
                "stock_change": str(stock_change),
            },
        )
    )
    await session.commit()
    logger.info("order_resolved actor=%s target=%s", actor.id, order.id)
    return await get_order(session, order.id)


PAYMENT_SELECT = """
    select
        p.id,
        p.order_id,
        o.reference as order_reference,
        o.status::text as order_status,
        buyer.name as buyer_name,
        p.amount,
        p.status::text as status,
        p.phone_number,
        p.stk_phone_number,
        p.mpesa_receipt_number,
        p.result_code,
        p.result_desc,
        p.environment,
        p.created_at,
        p.completed_at,
        p.callback_received_at,
        (
            select count(*)
            from public.payments as earlier
            where earlier.order_id = p.order_id
              and (earlier.created_at, earlier.id) <= (p.created_at, p.id)
        ) as attempt_number
    from public.payments as p
    join public.orders as o on o.id = p.order_id
    join public.users as buyer on buyer.id = o.buyer_id
"""

PAYMENT_COUNT = """
    select count(*)
    from public.payments as p
    join public.orders as o on o.id = p.order_id
"""


async def list_payments(
    session: AsyncSession,
    pagination: Pagination,
    *,
    status_filter: PaymentStatus | None = None,
    order_reference: str | None = None,
    environment: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> AdminPaymentPage:
    """Payments newest first; dates are calendar days in Africa/Nairobi. One count and one page query."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if status_filter is not None:
        clauses.append("p.status = cast(:status as public.payment_status)")
        params["status"] = status_filter.value
    if order_reference is not None:
        clauses.append("o.reference = :order_reference")
        params["order_reference"] = order_reference.strip().upper()
    if environment is not None:
        clauses.append("p.environment = :environment")
        params["environment"] = environment
    if created_from is not None:
        clauses.append("(p.created_at at time zone 'Africa/Nairobi')::date >= :created_from")
        params["created_from"] = created_from
    if created_to is not None:
        clauses.append("(p.created_at at time zone 'Africa/Nairobi')::date <= :created_to")
        params["created_to"] = created_to
    where = (" where " + " and ".join(clauses)) if clauses else ""
    total = int(await session.scalar(text(PAYMENT_COUNT + where), params) or 0)
    rows = (
        (
            await session.execute(
                text(PAYMENT_SELECT + where + " order by p.created_at desc, p.id desc limit :limit offset :offset"),
                {**params, "limit": pagination.limit, "offset": pagination.offset},
            )
        )
        .mappings()
        .all()
    )
    items = [
        AdminPaymentOut(
            id=row["id"],
            order_id=row["order_id"],
            order_reference=row["order_reference"],
            order_status=row["order_status"],
            buyer_name=row["buyer_name"],
            amount=int(row["amount"]),
            status=row["status"],
            phone_masked=mask_phone_or_none(row.get("phone_number")),
            stk_target_masked=mask_phone(row["stk_phone_number"]),
            mpesa_receipt_number=row["mpesa_receipt_number"],
            result_code=row["result_code"],
            result_desc=row["result_desc"],
            environment=row["environment"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            callback_received_at=row["callback_received_at"],
            attempt_number=int(row["attempt_number"]),
        )
        for row in rows
    ]
    return AdminPaymentPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


async def payment_events(session: AsyncSession, payment_id: UUID) -> PaymentEventList:
    """The event timeline of one payment, oldest first, capped at 100. Payloads were redacted when stored."""
    exists = await session.scalar(select(Payment.id).where(Payment.id == payment_id))
    if exists is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "payment_not_found", "Payment not found.")
    events = (
        await session.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at.asc(), PaymentEvent.id.asc())
            .limit(EVENTS_LIMIT)
        )
    ).all()
    return PaymentEventList(
        payment_id=payment_id,
        items=[
            PaymentEventOut(
                id=event.id,
                event_type=event.event_type,
                checkout_request_id=event.checkout_request_id,
                payload=dict(event.payload),
                created_at=event.created_at,
            )
            for event in events
        ],
        limit=EVENTS_LIMIT,
    )
