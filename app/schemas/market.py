"""Market and observed-price response schemas."""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator

from app.models.enums import CropType


def normalize_crop(value: object) -> object:
    """Trim and lowercase crop query values before enum validation."""
    return value.strip().lower() if isinstance(value, str) else value


CropQuery = Annotated[CropType, BeforeValidator(normalize_crop)]


class MarketRead(BaseModel):
    market_name: str


class MarketPriceRead(BaseModel):
    market: str
    crop: str
    month: date
    avg_price_per_kg: Decimal


class PriceTrend(BaseModel):
    market: str
    crop: str
    month: date
    avg_price_per_kg: Decimal
