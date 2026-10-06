"""Read-only market price service boundaries."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def markets(session: AsyncSession) -> list[Any]:
    """Return the migration-seeded market directory."""
    result = await session.execute(text("SELECT market_name FROM market_locations ORDER BY market_name"))
    return [dict(row) for row in result.mappings().all()]


async def current_prices(session: AsyncSession, crop: str) -> list[Any]:
    """Return each market's latest observed monthly price for the selected crop."""
    result = await session.execute(
        text("""
            SELECT DISTINCT ON (market) market, crop, month, avg_price_per_kg
            FROM market_prices
            WHERE crop = :crop
            ORDER BY market, month DESC
        """),
        {"crop": crop},
    )
    return [dict(row) for row in result.mappings().all()]


async def price_trends(session: AsyncSession, market: str, crop: str, months: int) -> list[Any]:
    """Return the trailing monthly price series in ascending month order."""
    result = await session.execute(
        text("""
            SELECT market, crop, month, avg_price_per_kg
            FROM market_prices
            WHERE market = :market AND crop = :crop
              AND month >= date_trunc('month', CURRENT_DATE) - (:months - 1) * interval '1 month'
            ORDER BY month
        """),
        {"market": market, "crop": crop, "months": months},
    )
    return [dict(row) for row in result.mappings().all()]
