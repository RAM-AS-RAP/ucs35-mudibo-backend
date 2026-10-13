"""Read-only forecast route."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.db import get_db
from app.models.enums import UserRole
from app.schemas.forecast import HorizonQuery
from app.schemas.market import CropQuery, MarketQuery
from app.services import forecast_service

router = APIRouter(
    prefix="/forecasts",
    tags=["forecasts"],
    dependencies=[Depends(require_role(UserRole.FARMER, UserRole.ADMIN))],
)


@router.get("")
async def get_forecast(
    market: MarketQuery,
    crop: CropQuery,
    horizon: HorizonQuery,
    session: AsyncSession = Depends(get_db),
) -> Any:
    return await forecast_service.latest_forecast(session, market, crop.value, horizon.value)
