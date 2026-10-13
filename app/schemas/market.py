"""Market and observed-price response schemas."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator

from app.models.enums import CropType
from app.schemas.common import DecimalOutput


def normalize_crop(value: object) -> object:
    """Trim and lowercase crop query values before enum validation."""
    return value.strip().lower() if isinstance(value, str) else value


CropQuery = Annotated[CropType, BeforeValidator(normalize_crop)]


def normalize_market(value: object) -> object:
    """Normalize supported market names to their database representation."""
    if not isinstance(value, str):
        return value
    market_names = {
        "nairobi": "Nairobi",
        "nakuru": "Nakuru",
        "mombasa": "Mombasa",
        "kisumu": "Kisumu",
    }
    normalized = value.strip().casefold()
    if normalized not in market_names:
        raise ValueError("Market must be Nairobi, Nakuru, Mombasa, or Kisumu")
    return market_names[normalized]


MarketName = Literal["Nairobi", "Nakuru", "Mombasa", "Kisumu"]
MarketQuery = Annotated[MarketName, BeforeValidator(normalize_market)]


class MarketRead(BaseModel):
    market_name: str


class PriceTrend(BaseModel):
    market: str
    crop: str
    points: list["PricePoint"]
    first_month: date | None
    last_month: date | None
    count: int


class PricePoint(DecimalOutput):
    month: date
    avg_price_per_kg: Decimal
    source: str


class MarketSummary(BaseModel):
    name: str
    latest_month: date


class CurrentPriceItem(DecimalOutput):
    market: str
    crop: str
    month: date
    avg_price_per_kg: Decimal
    source: str
    fetched_at: datetime
    previous_month: date | None
    previous_price: Decimal | None
    change_pct: Decimal | None


class CurrentPricesResponse(BaseModel):
    items: list[CurrentPriceItem]
    as_of_note: str
