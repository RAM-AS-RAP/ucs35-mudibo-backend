"""Administrator dashboard statistics and forecast accuracy routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.admin import AccuracyOut, StatsOut
from app.schemas.common import Pagination, pagination_params
from app.schemas.forecast import HorizonQuery
from app.schemas.market import CropQuery
from app.services import admin_report_service

router = APIRouter()


@router.get("/stats", response_model=StatsOut)
async def stats(session: AsyncSession = Depends(get_db)) -> StatsOut:
    return await admin_report_service.stats(session)


@router.get("/forecast-accuracy", response_model=AccuracyOut)
async def forecast_accuracy(
    market: str | None = Query(default=None, min_length=1, max_length=100),
    crop: CropQuery | None = None,
    horizon: HorizonQuery | None = None,
    is_backfill: bool | None = None,
    include_pending: bool = True,
    pagination: Pagination = Depends(pagination_params),
    session: AsyncSession = Depends(get_db),
) -> AccuracyOut:
    return await admin_report_service.forecast_accuracy(
        session,
        pagination,
        market=market,
        crop=crop,
        horizon=horizon,
        is_backfill=is_backfill,
        include_pending=include_pending,
    )
