"""Observed market price routes."""

from enum import IntEnum

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.db import get_db
from app.models.enums import UserRole
from app.schemas.market import CropQuery, CurrentPricesResponse, MarketQuery, PriceTrend
from app.services import price_service


class TrendMonths(IntEnum):
    """Trend windows offered to clients."""

    SIX = 6
    TWELVE = 12


router = APIRouter(
    prefix="/prices",
    tags=["prices"],
    dependencies=[Depends(require_role(UserRole.FARMER, UserRole.ADMIN))],
)


@router.get("/current")
async def current_prices(crop: CropQuery, session: AsyncSession = Depends(get_db)) -> CurrentPricesResponse:
    return await price_service.current_prices(session, crop.value)


@router.get("/trends")
async def price_trends(
    market: MarketQuery,
    crop: CropQuery,
    months: TrendMonths = Query(default=TrendMonths.TWELVE),
    session: AsyncSession = Depends(get_db),
) -> PriceTrend:
    return await price_service.price_trends(session, market, crop.value, int(months))
