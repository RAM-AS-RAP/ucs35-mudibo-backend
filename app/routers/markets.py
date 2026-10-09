"""Market directory routes."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_db
from app.services import price_service

router = APIRouter(prefix="/markets", tags=["markets"], dependencies=[Depends(get_current_user)])


@router.get("")
async def get_markets(session: AsyncSession = Depends(get_db)) -> list[Any]:
    return await price_service.markets(session)
