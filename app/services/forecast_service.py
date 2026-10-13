"""Read-only forecast view queries with source-price freshness checks."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.enums import CropType, ForecastHorizon
from app.schemas.forecast import ForecastRead
from app.services.market_service import current_month_nairobi, require_available_market


def _months_between(first: Any, second: Any) -> int:
    """Return calendar-month distance from second to first."""
    return (first.year - second.year) * 12 + first.month - second.month


async def latest_forecast(
    session: AsyncSession,
    market: str,
    crop: CropType | str,
    horizon: ForecastHorizon | str,
) -> ForecastRead:
    """Return a stored forecast only while its origin matches recent observed prices."""
    canonical_market = await require_available_market(session, market)
    crop_value = getattr(crop, "value", crop)
    horizon_value = getattr(horizon, "value", horizon)
    result = await session.execute(
        text("""
            SELECT market, crop, horizon, origin_month, target_month, origin_price,
                     predicted_price, predicted_log_change, shap_values, generated_at, is_backfill
            FROM latest_forecasts
            WHERE market = :market AND crop = :crop AND horizon = :horizon
        """),
        {"market": canonical_market, "crop": crop_value, "horizon": horizon_value},
    )
    row = result.mappings().first()
    if row is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            "forecast_unavailable",
            "Forecast not available yet for this market and crop.",
            reason="no_forecast",
        )

    latest_price_month = await session.scalar(
        text("""
            SELECT max(month)
            FROM market_prices
            WHERE market = :market AND crop = :crop
        """),
        {"market": canonical_market, "crop": crop_value},
    )
    if latest_price_month is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            "forecast_unavailable",
            "Forecast not available yet for this market and crop.",
            reason="no_forecast",
        )

    forecast = dict(row)
    current_month = current_month_nairobi()
    age_months = _months_between(current_month, latest_price_month)
    if forecast["is_backfill"] or forecast["origin_month"] != latest_price_month or not 0 <= age_months <= 2:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            "forecast_unavailable",
            "Forecast not available yet for this market and crop.",
            reason="stale",
        )

    predicted_log_change = Decimal(forecast["predicted_log_change"])
    expected_change_pct = ((predicted_log_change.exp() - Decimal("1")) * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    forecast["expected_change_pct"] = expected_change_pct
    forecast["based_on"] = forecast["origin_month"]
    forecast["for_month"] = forecast["target_month"]
    return ForecastRead.model_validate(forecast)
