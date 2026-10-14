"""Produce listing schemas."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from fastapi import Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

from app.models.enums import CropType
from app.schemas.common import DecimalOutput, Page
from app.schemas.market import CropQuery

MAX_PRICE_FILTER = Decimal("10000000")


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


class ListingSort(StrEnum):
    NEWEST = "newest"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"


class ListingBrowseFilters(BaseModel):
    """Validated buyer browse filters."""

    crop: CropType | None = None
    market: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    sort: ListingSort = ListingSort.NEWEST


def listing_browse_filters(
    crop: CropQuery | None = None,
    market: str | None = Query(default=None, min_length=1, max_length=100),
    min_price: Decimal | None = Query(default=None, ge=0, le=MAX_PRICE_FILTER),
    max_price: Decimal | None = Query(default=None, ge=0, le=MAX_PRICE_FILTER),
    sort: ListingSort = ListingSort.NEWEST,
) -> ListingBrowseFilters:
    """FastAPI dependency that validates browse query parameters, including the price range."""
    if min_price is not None and max_price is not None and min_price > max_price:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "min_price"),
                    "msg": "min_price must be less than or equal to max_price",
                }
            ]
        )
    return ListingBrowseFilters(crop=crop, market=market, min_price=min_price, max_price=max_price, sort=sort)


class PublicListingOut(DecimalOutput):
    """Buyer-facing listing; never includes the farmer's id or contact details."""

    id: UUID
    crop: str
    market: str
    quantity_available: Decimal
    unit: str
    price_per_unit: Decimal
    image_url: str | None
    seller_name: str
    status: str
    created_at: datetime


class PublicListingDetail(PublicListingOut):
    available: bool


class PublicListingPage(Page[PublicListingOut]):
    pass
