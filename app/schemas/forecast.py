"""Forecast and forecast-accuracy response schemas."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator

from app.models.enums import ForecastHorizon
from app.schemas.common import DecimalOutput


def normalize_horizon(value: object) -> object:
    """Accept numeric or legacy horizon names and return the database enum value."""
    if not isinstance(value, str):
        return value
    horizon_names = {
        "1": ForecastHorizon.ONE_MONTH,
        "one_month": ForecastHorizon.ONE_MONTH,
        "2": ForecastHorizon.TWO_MONTH,
        "two_month": ForecastHorizon.TWO_MONTH,
    }
    normalized = value.strip().casefold()
    if normalized not in horizon_names:
        raise ValueError("Horizon must be 1 or 2")
    return horizon_names[normalized]


HorizonQuery = Annotated[ForecastHorizon, BeforeValidator(normalize_horizon)]


class ForecastRead(DecimalOutput):
    market: str
    crop: str
    horizon: str
    origin_month: date
    target_month: date
    origin_price: Decimal
    predicted_price: Decimal
    predicted_log_change: Decimal
    expected_change_pct: Decimal
    shap_values: Any
    generated_at: datetime
    based_on: date
    for_month: date


class ForecastAccuracyRead(BaseModel):
    market: str
    crop: str
    horizon: str
    origin_month: date
    target_month: date
    origin_price: Decimal
    predicted_price: Decimal
    actual_price: Decimal | None
    abs_error: Decimal | None
    pct_error: Decimal | None
    direction_correct: bool | None
    beat_persistence: bool | None
    is_backfill: bool
    pending: bool
