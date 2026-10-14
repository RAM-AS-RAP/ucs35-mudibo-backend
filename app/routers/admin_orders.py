"""Administrator order and payment review routes, including reconciliation resolution."""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_admin
from app.db import get_db
from app.models.enums import OrderStatus, PaymentStatus
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.admin import (
    AdminOrderDetail,
    AdminOrderPage,
    AdminPaymentPage,
    PaymentEventList,
    ResolveOrderRequest,
    SearchQuery,
)
from app.schemas.common import Pagination, pagination_params
from app.services import admin_order_service

router = APIRouter()


@router.get("/orders", response_model=AdminOrderPage)
async def list_orders(
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
    q: SearchQuery = None,
    pagination: Pagination = Depends(pagination_params),
    session: AsyncSession = Depends(get_db),
) -> AdminOrderPage:
    return await admin_order_service.list_orders(session, pagination, status_filter=status_filter, q=q)


@router.get("/orders/{order_id}", response_model=AdminOrderDetail)
async def get_order(order_id: UUID, session: AsyncSession = Depends(get_db)) -> AdminOrderDetail:
    return await admin_order_service.get_order(session, order_id)


@router.post("/orders/{order_id}/resolve", response_model=AdminOrderDetail)
@limiter.limit("30/hour")
async def resolve_order(
    request: Request,
    order_id: UUID,
    body: ResolveOrderRequest,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminOrderDetail:
    del request
    return await admin_order_service.resolve_order(session, actor, order_id, body)


@router.get("/payments", response_model=AdminPaymentPage)
async def list_payments(
    status_filter: PaymentStatus | None = Query(default=None, alias="status"),
    order_reference: str | None = Query(default=None, min_length=6, max_length=12),
    environment: Literal["sandbox", "production"] | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    pagination: Pagination = Depends(pagination_params),
    session: AsyncSession = Depends(get_db),
) -> AdminPaymentPage:
    return await admin_order_service.list_payments(
        session,
        pagination,
        status_filter=status_filter,
        order_reference=order_reference,
        environment=environment,
        created_from=created_from,
        created_to=created_to,
    )


@router.get("/payments/{payment_id}/events", response_model=PaymentEventList)
async def payment_events(payment_id: UUID, session: AsyncSession = Depends(get_db)) -> PaymentEventList:
    return await admin_order_service.payment_events(session, payment_id)
