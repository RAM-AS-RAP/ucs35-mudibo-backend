"""Observed market price routes."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_db
from app.schemas.market import CropQuery, MarketQuery
from app.services import price_service

router = APIRouter(prefix="/prices", tags=["prices"], dependencies=[Depends(get_current_user)])


@router.get("/current")
async def current_prices(crop: CropQuery, session: AsyncSession = Depends(get_db)) -> list[Any]:
    return await price_service.current_prices(session, crop)


@router.get("/trends")
async def price_trends(
    market: MarketQuery,
    crop: CropQuery,
    months: int = Query(default=12, ge=1, le=12),
    session: AsyncSession = Depends(get_db),
) -> list[Any]:
    return await price_service.price_trends(session, market, crop, months)
