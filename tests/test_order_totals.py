"""Tests for server-side order total calculation."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.schemas.order import OrderRead
from app.services.order_service import calculate_total


def test_order_total_rounds_up_to_integer_shillings() -> None:
    assert calculate_total(Decimal("2.5"), Decimal("112.56")) == 282
    assert isinstance(calculate_total(Decimal("2.5"), Decimal("112.56")), int)


def test_whole_amount_is_not_increased() -> None:
    assert calculate_total(Decimal("2"), Decimal("50")) == 100


def test_order_money_serializes_as_rounded_json_numbers() -> None:
    order = OrderRead(
        id=uuid4(),
        reference="ABC12345",
        listing_id=uuid4(),
        buyer_id=uuid4(),
        quantity=Decimal("2.50"),
        unit_price=Decimal("112.555"),
        total_amount=Decimal("281.387"),
        status="pending_payment",
        expires_at=datetime.now(UTC),
    )
    body = json.loads(order.model_dump_json())
    assert body["unit_price"] == 112.56
    assert body["total_amount"] == 281.39
