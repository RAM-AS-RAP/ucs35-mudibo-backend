"""Buyer order routes."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, require_role
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.order import OrderCreate, OrderDetail, OrderRead
from app.schemas.payment import PaymentRead
from app.services import order_service
from app.services.payment_service import initiate_payment

router = APIRouter(prefix="/orders", tags=["orders"], dependencies=[Depends(get_current_user)])


@router.post("", status_code=201, response_model=OrderRead)
async def create_order(
    body: OrderCreate, buyer: User = Depends(require_role(UserRole.BUYER)), session: AsyncSession = Depends(get_db)
) -> Any:
    return await order_service.create_order(session, buyer, body.listing_id, body.quantity)


@router.get("/mine", response_model=list[OrderRead])
async def my_orders(
    buyer: User = Depends(require_role(UserRole.BUYER)), session: AsyncSession = Depends(get_db)
) -> Any:
    return await order_service.list_my_orders(session, buyer)


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: UUID,
    user: User = Depends(require_role(UserRole.BUYER, UserRole.ADMIN)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    return await order_service.get_order(session, order_id, user)


@router.post("/{order_id}/pay", response_model=PaymentRead)
async def pay_order(
    order_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
    buyer: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    return await initiate_payment(session, order_id, buyer, idempotency_key)
