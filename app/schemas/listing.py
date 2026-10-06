"""Produce listing schemas."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ListingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    farmer_id: UUID
    crop: str
    market: str
    quantity_available: Decimal
    unit: str
    price_per_unit: Decimal
    image_path: str | None
    status: str
    created_at: datetime


class ListingUpdate(BaseModel):
    quantity_available: Decimal | None = None
    price_per_unit: Decimal | None = None
    unit: str | None = None
