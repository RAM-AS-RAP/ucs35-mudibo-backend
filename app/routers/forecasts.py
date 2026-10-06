"""Read-only forecast route."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import must_change_password, require_role
from app.db import get_db
from app.models.enums import UserRole
from app.services import forecast_service

router = APIRouter(
    prefix="/forecasts",
    tags=["forecasts"],
    dependencies=[Depends(must_change_password), Depends(require_role(UserRole.FARMER))],
)


@router.get("")
async def get_forecast(market: str, crop: str, horizon: str, session: AsyncSession = Depends(get_db)) -> Any:
    return await forecast_service.latest_forecast(session, market, crop, horizon)
