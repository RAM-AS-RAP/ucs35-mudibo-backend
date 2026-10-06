"""Tests for Kenyan phone number normalization."""

import pytest

from app.utils.phone import normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "254712345678"),
        ("0112345678", "254112345678"),
        ("+254712345678", "254712345678"),
        ("254112345678", "254112345678"),
        ("0712 345-678", "254712345678"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "071234567", "0812345678", "+12025550123"])
def test_invalid_phone_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)
