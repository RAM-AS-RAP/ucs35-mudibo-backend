"""Market directory routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.db import get_db
from app.models.enums import UserRole
from app.schemas.market import MarketSummary
from app.services import price_service

router = APIRouter(
    prefix="/markets",
    tags=["markets"],
    dependencies=[Depends(require_role(UserRole.FARMER, UserRole.ADMIN))],
)


@router.get("")
async def get_markets(session: AsyncSession = Depends(get_db)) -> list[MarketSummary]:
    return await price_service.markets(session)
