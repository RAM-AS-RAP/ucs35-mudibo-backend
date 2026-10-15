"""Administrator user routes: list users, disable and enable accounts.

New admins are invited by email code (``admin_invites``); forgotten passwords are reset by the user
through the public email reset flow. No route returns a credential.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_admin
from app.core.errors import AppError
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.admin import SearchQuery, UserAdminOut, UserAdminPage
from app.schemas.common import Pagination, pagination_params
from app.services import admin_user_service

router = APIRouter()


async def require_empty_body(request: Request) -> None:
    if (await request.body()).strip():
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "body_not_allowed", "This endpoint does not accept a request body."
        )


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
