"""Administrator routes."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import must_change_password, require_role, require_super_admin
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.user import AdminCreate
from app.services import admin_service, listing_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(must_change_password)])


@router.post("/admins", status_code=201)
async def create_admin(
    body: AdminCreate, actor: User = Depends(require_super_admin), session: AsyncSession = Depends(get_db)
) -> Any:
    return await admin_service.create_admin(session, actor, body.model_dump())


@router.get("/users")
async def users(
    role: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_role(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    del actor
    return await admin_service.users(session, role=role, limit=limit, offset=offset)


@router.patch("/users/{user_id}/disable")
async def disable_user(
    user_id: UUID, actor: User = Depends(require_role(UserRole.ADMIN)), session: AsyncSession = Depends(get_db)
) -> Any:
    return await admin_service.set_user_disabled(session, actor, user_id, True)


@router.patch("/users/{user_id}/enable")
async def enable_user(
    user_id: UUID, actor: User = Depends(require_role(UserRole.ADMIN)), session: AsyncSession = Depends(get_db)
) -> Any:
    return await admin_service.set_user_disabled(session, actor, user_id, False)


@router.get("/listings")
async def listings(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_role(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    del actor
    return await admin_service.listings(session, limit=limit, offset=offset)


@router.delete("/listings/{listing_id}", status_code=204)
async def remove_listing(
    listing_id: UUID, actor: User = Depends(require_role(UserRole.ADMIN)), session: AsyncSession = Depends(get_db)
) -> None:
    await listing_service.remove_listing(session, actor, listing_id)


@router.get("/payments")
async def payments(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_role(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    del actor
    return await admin_service.payments(session, limit=limit, offset=offset)


@router.get("/stats")
async def stats(actor: User = Depends(require_role(UserRole.ADMIN)), session: AsyncSession = Depends(get_db)) -> Any:
    del actor
    return await admin_service.stats(session)


@router.get("/forecast-accuracy")
async def forecast_accuracy(
    market: str | None = None,
    crop: str | None = None,
    horizon: str | None = None,
    actor: User = Depends(require_role(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_db),
) -> Any:
    del actor
    return await admin_service.forecast_accuracy(session, market=market, crop=crop, horizon=horizon)
