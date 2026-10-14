"""Administrator user routes: create admins, list users, disable/enable, reset passwords."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_admin, require_super_admin
from app.core.errors import AppError
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.admin import (
    CreateAdminRequest,
    CreateAdminResponse,
    ResetPasswordResponse,
    SearchQuery,
    UserAdminOut,
    UserAdminPage,
)
from app.schemas.common import Pagination, pagination_params
from app.services import admin_user_service

router = APIRouter()

NO_STORE = "no-store"


async def require_empty_body(request: Request) -> None:
    if (await request.body()).strip():
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "body_not_allowed", "This endpoint does not accept a request body."
        )


@router.post("/admins", status_code=201, response_model=CreateAdminResponse)
@limiter.limit("10/hour")
async def create_admin(
    request: Request,
    response: Response,
    body: CreateAdminRequest,
    actor: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db),
) -> CreateAdminResponse:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await admin_user_service.create_admin(session, actor, body)


@router.get("/users", response_model=UserAdminPage)
async def list_users(
    role: UserRole | None = None,
    is_disabled: bool | None = None,
    q: SearchQuery = None,
    pagination: Pagination = Depends(pagination_params),
    session: AsyncSession = Depends(get_db),
) -> UserAdminPage:
    return await admin_user_service.list_users(session, pagination, role=role, is_disabled=is_disabled, q=q)


@router.patch("/users/{user_id}/disable", response_model=UserAdminOut, dependencies=[Depends(require_empty_body)])
@limiter.limit("30/hour")
async def disable_user(
    request: Request,
    user_id: UUID,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserAdminOut:
    del request
    return await admin_user_service.set_user_disabled(session, actor, user_id, True)


@router.patch("/users/{user_id}/enable", response_model=UserAdminOut, dependencies=[Depends(require_empty_body)])
@limiter.limit("30/hour")
async def enable_user(
    request: Request,
    user_id: UUID,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserAdminOut:
    del request
    return await admin_user_service.set_user_disabled(session, actor, user_id, False)


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    dependencies=[Depends(require_empty_body)],
)
@limiter.limit("10/hour")
async def reset_password(
    request: Request,
    response: Response,
    user_id: UUID,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ResetPasswordResponse:
    del request
    response.headers["Cache-Control"] = NO_STORE
    temporary_password = await admin_user_service.reset_password(session, actor, user_id)
    return ResetPasswordResponse(temporary_password=temporary_password)
