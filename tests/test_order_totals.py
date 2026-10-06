"""Tests for server-side order total calculation."""

from decimal import Decimal

from app.services.order_service import calculate_total


def test_order_total_rounds_up_to_integer_shillings() -> None:
    assert calculate_total(Decimal("2.5"), Decimal("112.56")) == 282
    assert isinstance(calculate_total(Decimal("2.5"), Decimal("112.56")), int)


def test_whole_amount_is_not_increased() -> None:
    assert calculate_total(Decimal("2"), Decimal("50")) == 100
