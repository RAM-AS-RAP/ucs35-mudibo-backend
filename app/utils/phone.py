"""Kenyan phone-number normalization and validation."""

import re

_PHONE_PATTERN = re.compile(r"^254[17][0-9]{8}$")


def normalize_phone(raw: str) -> str:
    """Normalize accepted Kenyan mobile formats to 254XXXXXXXXX."""
    value = re.sub(r"[\s-]", "", raw)
    if value.startswith("+"):
        value = value[1:]
    if value.startswith("0") and len(value) == 10:
        value = "254" + value[1:]
    if not _PHONE_PATTERN.fullmatch(value):
        raise ValueError("Enter a valid Kenyan mobile phone number")
    return value
