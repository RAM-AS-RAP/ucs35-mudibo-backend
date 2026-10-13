"""Produce listing schemas."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

from app.schemas.common import DecimalOutput, Page


class ListingCreateForm(BaseModel):
    """Accepted multipart fields for creating a farmer listing."""

    model_config = ConfigDict(extra="forbid")

    crop: str
    market: str
    quantity_available: str
    unit: str = "kg"
    price_per_unit: str


class ListingEditForm(BaseModel):
    """Accepted multipart fields for editing a farmer listing."""

    model_config = ConfigDict(extra="forbid")

    quantity_available: str | None = None
    price_per_unit: str | None = None
    remove_image: bool | None = None


async def _reject_unknown_form_fields(request: Request, allowed: set[str]) -> None:
    """Reject multipart keys not explicitly exposed by the route."""
    form_data = await request.form()
    unknown = sorted(set(form_data.keys()) - allowed)
    if unknown:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("body", field), "msg": "Extra inputs are not permitted"}
                for field in unknown
            ]
        )


async def validate_listing_create_form(request: Request) -> None:
    """Reject unknown multipart fields for listing creation."""
    await _reject_unknown_form_fields(
        request, {"crop", "market", "quantity_available", "unit", "price_per_unit", "image"}
    )


async def validate_listing_edit_form(request: Request) -> None:
    """Reject unknown or immutable multipart fields for listing edits."""
    await _reject_unknown_form_fields(request, {"quantity_available", "price_per_unit", "image", "remove_image"})


class ListingOut(DecimalOutput):
    id: UUID
    farmer_id: UUID
    crop: str
    market: str
    quantity_available: Decimal
    unit: str
    price_per_unit: Decimal
    image_url: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ListingUpdate(BaseModel):
    quantity_available: Decimal | None = None
    price_per_unit: Decimal | None = None
    unit: str | None = None


class FarmerOrderOut(DecimalOutput):
    order_id: UUID
    reference: str
    listing_id: UUID
    crop: str
    market: str
    quantity: Decimal
    unit_price: Decimal
    total_amount: Decimal
    status: str
    created_at: datetime
    buyer_name: str
    payment_status: str | None
    payment_environment: str | None
    mpesa_receipt_number: str | None
    payment_completed_at: datetime | None


class FarmerOrdersPage(Page[FarmerOrderOut]):
    note: str


class ListingPage(Page[ListingOut]):
    pass
