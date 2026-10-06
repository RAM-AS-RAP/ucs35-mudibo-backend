"""Forecast view query service boundaries."""

from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def latest_forecast(session: AsyncSession, market: str, crop: str, horizon: str) -> Any:
    """Read one latest-forecast view row without recomputing any predictions."""
    result = await session.execute(
        text("""
            SELECT market, crop, horizon, origin_month, target_month, origin_price,
                   predicted_price, predicted_log_change, shap_values
            FROM latest_forecasts
            WHERE market = :market AND crop = :crop AND horizon = :horizon
        """),
        {"market": market, "crop": crop, "horizon": horizon},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "forecast_unavailable", "message": "Forecast is not available yet"}
        )
    return dict(row)
