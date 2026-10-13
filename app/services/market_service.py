"""Shared helpers for markets with published price data."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError


def current_month_nairobi(now: datetime | None = None) -> date:
    """Return the first day of the current calendar month in Africa/Nairobi."""
    local_now = (
        now.astimezone(ZoneInfo("Africa/Nairobi")) if now is not None else datetime.now(ZoneInfo("Africa/Nairobi"))
    )
    return local_now.date().replace(day=1)


async def get_available_markets(session: AsyncSession) -> list[str]:
    """Return markets that actually have price observations, sorted by name."""
    result = await session.execute(text("SELECT DISTINCT market FROM market_prices ORDER BY market"))
    return [str(row[0]) for row in result.all()]


async def require_available_market(session: AsyncSession, market: str) -> str:
    """Require a market with published prices and return its canonical database name."""
    markets = await get_available_markets(session)
    canonical = {name.casefold(): name for name in markets}.get(market.strip().casefold())
    if canonical is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "market_not_available", "Market is not available.")
    return canonical
