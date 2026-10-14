"""Farmer-owned listing APIs and buyer listing views.

Roles are enforced per route. Static paths (`/mine`, `/mine/orders`) are declared before the
dynamic `/{listing_id}` routes, and `listing_id` is typed as UUID, so `mine` is never parsed as an id.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.db import get_db
from app.models.enums import ListingStatus, OrderStatus, UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.common import Pagination, pagination_params
from app.schemas.listing import (
    FarmerOrdersPage,
    ListingBrowseFilters,
    ListingOut,
    ListingPage,
    PublicListingDetail,
    PublicListingPage,
    listing_browse_filters,
    validate_listing_create_form,
    validate_listing_edit_form,
)
from app.services import listing_service, order_service

router = APIRouter(prefix="/listings", tags=["listings"])


@router.post("", status_code=201, response_model=ListingOut)
@limiter.limit("30/hour")
async def create_listing(
    request: Request,
    crop: str = Form(),
    market: str = Form(),
    quantity_available: str = Form(),
    unit: str = Form(default="kg"),
    price_per_unit: str = Form(),
    image: UploadFile | None = File(default=None),
    farmer: User = Depends(require_role(UserRole.FARMER)),
    _: None = Depends(validate_listing_create_form),
    session: AsyncSession = Depends(get_db),
) -> ListingOut:
    del request
    values = {
        "crop": crop,
        "market": market,
        "quantity_available": quantity_available,
        "unit": unit,
        "price_per_unit": price_per_unit,
    }
    return await listing_service.create_listing(session, farmer, values, image)


@router.get("/mine", response_model=ListingPage)
async def my_listings(
    status_filter: ListingStatus | None = Query(default=None, alias="status"),
    pagination: Pagination = Depends(pagination_params),
    farmer: User = Depends(require_role(UserRole.FARMER)),
    session: AsyncSession = Depends(get_db),
) -> ListingPage:
    return await listing_service.my_listings(session, farmer, pagination, status_filter)


@router.get("/mine/orders", response_model=FarmerOrdersPage)
async def farmer_orders(
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
    listing_id: UUID | None = None,
    pagination: Pagination = Depends(pagination_params),
    farmer: User = Depends(require_role(UserRole.FARMER)),
    session: AsyncSession = Depends(get_db),
) -> FarmerOrdersPage:
    return await order_service.list_orders_for_farmer(session, farmer, pagination, status_filter, listing_id)


@router.get("", response_model=PublicListingPage)
async def browse_listings(
    filters: ListingBrowseFilters = Depends(listing_browse_filters),
    pagination: Pagination = Depends(pagination_params),
    _: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
) -> PublicListingPage:
    return await listing_service.browse_listings(session, filters, pagination)


@router.get("/{listing_id}", response_model=PublicListingDetail)
async def listing_detail(
    listing_id: UUID,
    _: User = Depends(require_role(UserRole.BUYER)),
    session: AsyncSession = Depends(get_db),
) -> PublicListingDetail:
    return await listing_service.get_public_listing(session, listing_id)


@router.patch("/{listing_id}", response_model=ListingOut)
@limiter.limit("30/hour")
async def update_listing(
    request: Request,
    listing_id: UUID,
    quantity_available: str | None = Form(default=None),
    price_per_unit: str | None = Form(default=None),
    image: UploadFile | None = File(default=None),
    remove_image: bool = Form(default=False),
    farmer: User = Depends(require_role(UserRole.FARMER)),
    _: None = Depends(validate_listing_edit_form),
    session: AsyncSession = Depends(get_db),
) -> ListingOut:
    del request
    values = {
        key: value
        for key, value in {
            "quantity_available": quantity_available,
            "price_per_unit": price_per_unit,
            "remove_image": remove_image,
        }.items()
        if value is not None
    }
    return await listing_service.update_listing(session, farmer, listing_id, values, image)


@router.delete("/{listing_id}", status_code=204)
async def delete_listing(
    listing_id: UUID,
    farmer: User = Depends(require_role(UserRole.FARMER)),
    session: AsyncSession = Depends(get_db),
) -> None:
    await listing_service.remove_listing(session, farmer, listing_id)
