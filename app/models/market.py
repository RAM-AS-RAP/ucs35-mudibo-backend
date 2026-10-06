"""Market location and observed-price table mappings."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.models.enums import CropType, PriceSource, crop_type_type, price_source_type


class MarketLocation(Base):
    __tablename__ = "market_locations"

    market_name: Mapped[str] = mapped_column(Text, primary_key=True)
    fews_market_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class MarketPrice(Base):
    __tablename__ = "market_prices"

    market: Mapped[str] = mapped_column(Text, ForeignKey("market_locations.market_name"), primary_key=True)
    crop: Mapped[CropType] = mapped_column(crop_type_type, primary_key=True)
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    avg_price_per_kg: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    source: Mapped[PriceSource] = mapped_column(price_source_type, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
