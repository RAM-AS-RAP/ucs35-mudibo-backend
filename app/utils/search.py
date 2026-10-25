"""Helpers for safe free-text search with LIKE/ILIKE."""

LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so ``%`` and ``_`` in user input match literally (use with ``escape='\\'``)."""
    return value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")


def contains_pattern(value: str) -> str:
    return f"%{escape_like(value.strip())}%"


def prefix_pattern(value: str) -> str:
    return f"{escape_like(value.strip())}%"
