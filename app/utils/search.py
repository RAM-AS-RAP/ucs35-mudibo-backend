"""Helpers for safe free-text search with LIKE/ILIKE."""

import re

LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so ``%`` and ``_`` in user input match literally (use with ``escape='\\'``)."""
    return value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")


def contains_pattern(value: str) -> str:
    return f"%{escape_like(value.strip())}%"


def prefix_pattern(value: str) -> str:
    return f"{escape_like(value.strip())}%"


def phone_prefix(value: str) -> str | None:
    """Digits of a phone-like ``value`` as a stored-phone prefix (a leading 0 becomes 254), else None."""
    if not re.fullmatch(r"[\d\s+()-]+", value.strip()):
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    return digits
