"""Marketplace order schemas."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrderCreate(BaseModel):
    listing_id: UUID
    quantity: Decimal = Field(gt=0)


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reference: str
    listing_id: UUID
    buyer_id: UUID
    quantity: Decimal
    unit_price: Decimal
    total_amount: int
    status: str
    expires_at: datetime


class OrderWithPayment(OrderRead):
    latest_payment: dict[str, object] | None = None


class OrderDetail(BaseModel):
    order: OrderRead
    latest_payment: dict[str, object] | None = None
