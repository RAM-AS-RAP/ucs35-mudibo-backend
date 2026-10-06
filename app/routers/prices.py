"""Observed market price routes."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import must_change_password
from app.db import get_db
from app.services import price_service

router = APIRouter(prefix="/prices", tags=["prices"], dependencies=[Depends(must_change_password)])


@router.get("/current")
async def current_prices(crop: str, session: AsyncSession = Depends(get_db)) -> list[Any]:
    return await price_service.current_prices(session, crop)


@router.get("/trends")
async def price_trends(
    market: str, crop: str, months: int = Query(default=24, ge=1, le=60), session: AsyncSession = Depends(get_db)
) -> list[Any]:
    return await price_service.price_trends(session, market, crop, months)
