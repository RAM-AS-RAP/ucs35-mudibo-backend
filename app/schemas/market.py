"""Market and observed-price response schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


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
