"""Common pagination and JSON money serialization schemas."""

from decimal import ROUND_HALF_UP, Decimal

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, field_serializer


class Page[T](BaseModel):
    """Paginated result envelope."""

    items: list[T]
    total: int
    limit: int
    offset: int


class Pagination(BaseModel):
    """Validated pagination parameters."""

    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


def pagination_params(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Pagination:
    """FastAPI dependency for bounded pagination parameters."""
    return Pagination(limit=limit, offset=offset)


class DecimalOutput(BaseModel):
    """Base schema that emits Decimal values as rounded JSON numbers."""

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def serialize_decimal(self, value: object) -> object:
        if isinstance(value, Decimal):
            return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        return value
