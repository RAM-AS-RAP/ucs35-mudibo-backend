"""Administrator listing moderation routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_admin
from app.db import get_db
from app.models.enums import ListingStatus
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.admin import AdminListingPage, SearchQuery
from app.schemas.common import Pagination, pagination_params
from app.schemas.market import CropQuery
from app.services import admin_listing_service

router = APIRouter()


@router.get("/listings", response_model=AdminListingPage)
async def list_listings(
    status_filter: ListingStatus | None = Query(default=None, alias="status"),
    crop: CropQuery | None = None,
    market: str | None = Query(default=None, min_length=1, max_length=100),
    farmer_id: UUID | None = None,
    q: SearchQuery = None,
    pagination: Pagination = Depends(pagination_params),
    session: AsyncSession = Depends(get_db),
) -> AdminListingPage:
    return await admin_listing_service.list_listings(
        session, pagination, status_filter=status_filter, crop=crop, market=market, farmer_id=farmer_id, q=q
    )


@router.delete("/listings/{listing_id}", status_code=204)
@limiter.limit("30/hour")
async def remove_listing(
    request: Request,
    listing_id: UUID,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> Response:
    del request
    await admin_listing_service.remove_listing(session, actor, listing_id)
    return Response(status_code=204)
