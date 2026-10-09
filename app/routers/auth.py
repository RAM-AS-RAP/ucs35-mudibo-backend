"""Registration and authentication routes."""

from fastapi import APIRouter, Depends, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user_any
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import ChangePasswordRequest, LoginRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserOut
from app.services import auth_service

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/auth", tags=["auth"])


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


@router.post("/change-password", status_code=204, response_class=Response)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user_any),
    session: AsyncSession = Depends(get_db),
) -> Response:
    await auth_service.change_password(session, user, body.current_password, body.new_password)
    return Response(status_code=204)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user_any)) -> User:
    return user
