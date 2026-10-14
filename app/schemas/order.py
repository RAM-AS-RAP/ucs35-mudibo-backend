"""Marketplace order schemas."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict

from app.core.config import get_settings
from app.schemas.common import DecimalOutput, Page

MAX_ORDER_QUANTITY = Decimal("100000")
ORDER_NOTE = (
    "Payments run in the Safaricom sandbox. A paid order is a simulated confirmation, no real money moves, "
    "and delivery is arranged outside the platform."
)


def parse_order_quantity(value: object) -> Decimal:
    """Parse a finite, positive, two-place order quantity within the configured bounds."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Quantity must be a decimal number")
    try:
        quantity = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Quantity must be a decimal number") from exc
    if not quantity.is_finite():
        raise ValueError("Quantity must be finite")
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")
    if max(0, -quantity.as_tuple().exponent) > 2:
        raise ValueError("Quantity may have at most two decimal places")
    minimum = get_settings().min_order_quantity
    if quantity < minimum:
        raise ValueError(f"Quantity must be at least {minimum}")
    if quantity > MAX_ORDER_QUANTITY:
        raise ValueError("Quantity must not exceed 100000")
    return quantity.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


OrderQuantity = Annotated[Decimal, BeforeValidator(parse_order_quantity)]


class OrderCreate(BaseModel):
    """Checkout body; the server derives buyer, price, total, and status."""

    model_config = ConfigDict(extra="forbid")

    listing_id: UUID
    quantity: OrderQuantity


class OrderRead(DecimalOutput):
    id: UUID
    reference: str
    listing_id: UUID
    buyer_id: UUID
    quantity: Decimal
    unit_price: Decimal
    total_amount: Decimal
    status: str
    expires_at: datetime


class OrderListingSummary(BaseModel):
    id: UUID
    crop: str
    market: str
    unit: str
    image_url: str | None
    seller_name: str
    status: str


class OrderPaymentSummary(BaseModel):
    """Latest payment of an order, without identifiers, phone numbers, or callback data."""

    status: str
    mpesa_receipt_number: str | None
    result_desc: str | None
    completed_at: datetime | None
    attempts: int


class OrderOut(DecimalOutput):
    id: UUID
    reference: str
    status: str
    is_expired: bool
    quantity: Decimal
    unit_price: Decimal
    total_amount: int
    expires_at: datetime
    server_time: datetime
    created_at: datetime
    can_pay: bool
    can_cancel: bool
    listing: OrderListingSummary
    payment: OrderPaymentSummary | None
    note: str = ORDER_NOTE


class OrderPage(Page[OrderOut]):
    pass
