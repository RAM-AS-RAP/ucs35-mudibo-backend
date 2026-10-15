"""Registration, authentication and invite acceptance routes."""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user_any
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    PasswordChangedResponse,
    RegisterRequest,
    TokenResponse,
)
from app.schemas.invite import AcceptedOut, AcceptInviteRequest, EmailRequest
from app.schemas.user import UserOut
from app.services import auth_service, invite_service

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/auth", tags=["auth"])

NO_STORE = "no-store"
INVITE_CODE_REQUESTED = "If an invitation exists for this address, a new code has been sent."
PASSWORD_CHANGED = "Password changed. Log in again: your current access token is no longer accepted."


@router.post("/register/farmer", response_model=UserOut, status_code=201)
@limiter.limit("10/hour")
async def register_farmer(request: Request, body: RegisterRequest, session: AsyncSession = Depends(get_db)) -> User:
    del request
    return await auth_service.register_user(session, body, role=UserRole.FARMER)


@router.post("/register/buyer", response_model=UserOut, status_code=201)
@limiter.limit("10/hour")
async def register_buyer(request: Request, body: RegisterRequest, session: AsyncSession = Depends(get_db)) -> User:
    del request
    return await auth_service.register_user(session, body, role=UserRole.BUYER)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    del request
    return await auth_service.login(session, body.identifier, body.password)


@router.post("/change-password", response_model=PasswordChangedResponse)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user_any),
    session: AsyncSession = Depends(get_db),
) -> PasswordChangedResponse:
    await auth_service.change_password(session, user, body.current_password, body.new_password)
    return PasswordChangedResponse(message=PASSWORD_CHANGED, login_required=True)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user_any)) -> User:
    return user


@router.post("/accept-invite", response_model=UserOut, status_code=201)
@limiter.limit("5/minute")
async def accept_invite(
    request: Request, response: Response, body: AcceptInviteRequest, session: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await invite_service.accept_invite(session, body)


@router.post("/invite/request-code", response_model=AcceptedOut, status_code=202)
@limiter.limit("3/hour")
async def request_invite_code(
    request: Request,
    body: EmailRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> AcceptedOut:
    del request
    await invite_service.request_invite_code(session, body.email, background_tasks)
    return AcceptedOut(message=INVITE_CODE_REQUESTED)
