"""Marketplace listing routes."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, must_change_password, require_role
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.services import listing_service

router = APIRouter(prefix="/listings", tags=["listings"], dependencies=[Depends(must_change_password)])


@router.post("", status_code=201)
async def create_listing(
    crop: str = Form(),
    market: str = Form(),
    quantity_available: str = Form(),
    unit: str = Form(default="kg"),
    price_per_unit: str = Form(),
    image: UploadFile | None = File(default=None),
    farmer: User = Depends(require_role(UserRole.FARMER)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    return await listing_service.create_listing(
        session,
        farmer,
        {
            "crop": crop,
            "market": market,
            "quantity_available": quantity_available,
            "unit": unit,
            "price_per_unit": price_per_unit,
        },
        image,
    )


@router.get("")
async def list_listings(
    crop: str | None = None,
    market: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    del user
    return await listing_service.list_listings(session, crop=crop, market=market, limit=limit, offset=offset)


@router.get("/mine")
async def my_listings(
    farmer: User = Depends(require_role(UserRole.FARMER)), session: AsyncSession = Depends(get_db)
) -> Any:
    return await listing_service.my_listings(session, farmer)


@router.get("/mine/orders")
async def farmer_orders(
    farmer: User = Depends(require_role(UserRole.FARMER)), session: AsyncSession = Depends(get_db)
) -> Any:
    return await listing_service.farmer_orders(session, farmer)


@router.get("/{listing_id}")
async def get_listing(
    listing_id: UUID, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Any:
    del user
    return await listing_service.get_listing(session, listing_id)


@router.patch("/{listing_id}")
async def update_listing(
    listing_id: UUID,
    quantity_available: Decimal | None = Form(default=None),
    price_per_unit: Decimal | None = Form(default=None),
    unit: str | None = Form(default=None),
    image: UploadFile | None = File(default=None),
    farmer: User = Depends(require_role(UserRole.FARMER)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    values = {
        key: value
        for key, value in {
            "quantity_available": quantity_available,
            "price_per_unit": price_per_unit,
            "unit": unit,
        }.items()
        if value is not None
    }
    return await listing_service.update_listing(session, farmer, listing_id, values, image)


@router.delete("/{listing_id}", status_code=204)
async def delete_listing(
    listing_id: UUID, farmer: User = Depends(require_role(UserRole.FARMER)), session: AsyncSession = Depends(get_db)
) -> None:
    await listing_service.remove_listing(session, farmer, listing_id)
