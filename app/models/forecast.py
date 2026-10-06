"""Forecast table mapping; forecasts are written by the external ML pipeline."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.models.enums import CropType, ForecastHorizon, crop_type_type, forecast_horizon_type


class Forecast(Base):
    __tablename__ = "forecasts"

    market: Mapped[str] = mapped_column(ForeignKey("market_locations.market_name"), primary_key=True)
    crop: Mapped[CropType] = mapped_column(crop_type_type, primary_key=True)
    horizon: Mapped[ForecastHorizon] = mapped_column(forecast_horizon_type, primary_key=True)
    origin_month: Mapped[date] = mapped_column(Date, primary_key=True)
    target_month: Mapped[date] = mapped_column(Date, nullable=False)
    origin_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    predicted_log_change: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    predicted_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    shap_values: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    is_backfill: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
