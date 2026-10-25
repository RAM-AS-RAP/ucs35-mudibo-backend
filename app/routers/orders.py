"""Buyer order routes: checkout, history, detail, cancel, and pay."""

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.db import get_db, get_session_factory
from app.integrations.daraja import DarajaClient, get_daraja_client
from app.models.enums import OrderStatus, UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.common import Pagination, pagination_params
from app.schemas.order import OrderCreate, OrderOut, OrderPage, PaymentStartOut
from app.schemas.payment import PayRequest
from app.services import order_service, payment_service

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
    session_factory: payment_service.SessionFactory = Depends(get_session_factory),
    daraja_client: DarajaClient | None = Depends(get_daraja_client),
) -> OrderOut:
    return await order_service.get_order(
        session, order_id, user, session_factory=session_factory, daraja_client=daraja_client
    )


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


@router.post(
    "/{order_id}/pay",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PaymentStartOut,
    # The body is required; a missing one is answered with 422 phone_required by the service.
    openapi_extra={"requestBody": {"required": True}},
)
@limiter.limit("10/minute")
async def pay_order(
    request: Request,
    order_id: UUID,
    body: PayRequest | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    buyer: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
    session_factory: payment_service.SessionFactory = Depends(get_session_factory),
    daraja_client: DarajaClient | None = Depends(get_daraja_client),
) -> PaymentStartOut:
    """Send an M-Pesa STK prompt for the buyer's order to the number entered for this payment.

    The body ``{"phone_number": "..."}`` is required: a missing body, a missing field or an empty value
    gives 422 ``phone_required``. The amount always comes from the server.
    """
    del request
    raw_key = payment_service.validate_idempotency_key(idempotency_key)
    phone_number = body.phone_number if body is not None else None
    started = await payment_service.initiate_payment(
        session, session_factory, daraja_client, buyer, order_id, raw_key, phone_number
    )
    order = await order_service.order_view(session, order_id, buyer)
    return PaymentStartOut(**order.model_dump(), message=started.message, prompt=started.prompt)
