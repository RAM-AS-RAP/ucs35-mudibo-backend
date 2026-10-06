"""Forecast and forecast-accuracy response schemas."""

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class ForecastRead(BaseModel):
    market: str
    crop: str
    horizon: str
    origin_month: date
    target_month: date
    origin_price: Decimal
    predicted_price: Decimal
    predicted_log_change: Decimal
    shap_values: Any


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
