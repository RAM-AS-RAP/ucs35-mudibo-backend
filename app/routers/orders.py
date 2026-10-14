"""Buyer order routes: checkout, history, detail, and cancel.

The pay endpoint arrives in Module 5.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.db import get_db
from app.models.enums import OrderStatus, UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.common import Pagination, pagination_params
from app.schemas.order import OrderCreate, OrderOut, OrderPage
from app.services import order_service

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", status_code=201, response_model=OrderOut)
@limiter.limit("30/hour")
async def create_order(
    request: Request,
    body: OrderCreate,
    buyer: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
) -> OrderOut:
    del request
    return await order_service.create_order(session, buyer, body.listing_id, body.quantity)


@router.get("/mine", response_model=OrderPage)
async def my_orders(
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
    pagination: Pagination = Depends(pagination_params),
    buyer: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
) -> OrderPage:
    return await order_service.list_my_orders(session, buyer, pagination, status_filter)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: UUID,
    user: User = Depends(require_role(UserRole.BUYER, UserRole.ADMIN)),
    session: AsyncSession = Depends(get_db),
) -> OrderOut:
    return await order_service.get_order(session, order_id, user)


@router.post("/{order_id}/cancel", response_model=OrderOut)
@limiter.limit("30/hour")
async def cancel_order(
    request: Request,
    order_id: UUID,
    buyer: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
) -> OrderOut:
    del request
    return await order_service.cancel_order(session, buyer, order_id)
