"""Read-only services for markets and monthly FEWS price observations."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.market import (
    CurrentPriceItem,
    CurrentPricesResponse,
    MarketSummary,
    PricePoint,
    PriceTrend,
)
from app.services.market_service import current_month_nairobi, require_available_market


def _month_offset(month: date, months_back: int) -> date:
    """Return the first day of a calendar month a fixed number of months earlier."""
    index = month.year * 12 + month.month - 1 - months_back
    year, zero_based_month = divmod(index, 12)
    return date(year, zero_based_month + 1, 1)


async def markets(session: AsyncSession) -> list[MarketSummary]:
    """Return only markets with price data and their newest published month."""
    result = await session.execute(
        text("""
            SELECT market AS name, max(month) AS latest_month
            FROM market_prices
            GROUP BY market
            ORDER BY market
        """)
    )
    return [MarketSummary(**dict(row)) for row in result.mappings().all()]


async def current_prices(session: AsyncSession, crop: str) -> CurrentPricesResponse:
    """Return each market's newest crop price and the immediately preceding month, if present."""
    result = await session.execute(
        text("""
            WITH latest AS (
                SELECT DISTINCT ON (market)
                    market, crop, month, avg_price_per_kg, source, fetched_at
                FROM market_prices
                WHERE crop = :crop
                ORDER BY market, month DESC
            )
            SELECT
                current.market,
                current.crop,
                current.month,
                current.avg_price_per_kg,
                current.source,
                current.fetched_at,
                previous.month AS previous_month,
                previous.avg_price_per_kg AS previous_price
            FROM latest AS current
            LEFT JOIN market_prices AS previous
              ON previous.market = current.market
             AND previous.crop = current.crop
             AND previous.month = (current.month - interval '1 month')::date
            ORDER BY current.market
        """),
        {"crop": crop},
    )
    items: list[CurrentPriceItem] = []
    for row in result.mappings().all():
        values = dict(row)
        previous_price = values["previous_price"]
        current_price = values["avg_price_per_kg"]
        values["change_pct"] = (
            (((current_price / previous_price) - Decimal("1")) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if previous_price is not None
            else None
        )
        items.append(CurrentPriceItem(**values))
    return CurrentPricesResponse(
        items=items,
        as_of_note="Prices are monthly figures from FEWS NET; the newest published month is shown.",
    )


async def price_trends(session: AsyncSession, market: str, crop: str, months: int) -> PriceTrend:
    """Return available observations in the trailing Nairobi calendar-month window."""
    canonical_market = await require_available_market(session, market)
    current_month = current_month_nairobi()
    first_month = _month_offset(current_month, months - 1)
    result = await session.execute(
        text("""
            SELECT month, avg_price_per_kg, source
            FROM market_prices
            WHERE market = :market
              AND crop = :crop
              AND month >= :first_month
              AND month <= :current_month
            ORDER BY month ASC
        """),
        {
            "market": canonical_market,
            "crop": crop,
            "first_month": first_month,
            "current_month": current_month,
        },
    )
    points = [PricePoint(**dict(row)) for row in result.mappings().all()]
    return PriceTrend(
        market=canonical_market,
        crop=crop,
        points=points,
        first_month=points[0].month if points else None,
        last_month=points[-1].month if points else None,
        count=len(points),
    )
