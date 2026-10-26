"""Registration, sign in (password or Google), invite acceptance and password reset routes.

Every sign in endpoint answers with ``AuthResult`` (``POST /auth/google`` with ``GoogleAuthResult``, which
adds the ``role_required`` status); fields that do not apply are omitted. Responses
that carry a token or a challenge are marked ``Cache-Control: no-store`` (the middleware also sets it
on every response).
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_result import AuthResult, GoogleAuthResult
from app.core.dependencies import get_current_user_any
from app.db import get_db
from app.integrations.google import GoogleVerifier, get_google_verifier
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    GoogleNonceOut,
    GoogleSignInRequest,
    GoogleSignupRequest,
    LoginRequest,
    LoginResendOut,
    LoginResendRequest,
    LoginVerifyRequest,
    PasswordChangedResponse,
    RegisterRequest,
    RegisterResendRequest,
    RegisterVerifyRequest,
    RegistrationAccepted,
    ResendAccepted,
)
from app.schemas.invite import AcceptedOut, AcceptInviteRequest, EmailRequest, PasswordResetConfirmRequest
from app.schemas.user import UserOut
from app.services import (
    auth_service,
    google_auth_service,
    invite_service,
    password_reset_service,
    registration_service,
)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/auth", tags=["auth"])

NO_STORE = "no-store"
INVITE_CODE_REQUESTED = "If an invitation exists for this address, a new code has been sent."
RESET_CODE_REQUESTED = "If an account with this email exists, a reset code has been sent."
PASSWORD_CHANGED = "Password changed. Log in again: your current access token is no longer accepted."


# Registration ----------------------------------------------------------------------------------


@router.post("/register/farmer", response_model=RegistrationAccepted, status_code=202)
@limiter.limit("10/hour")
async def register_farmer(
    request: Request,
    response: Response,
    body: RegisterRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> RegistrationAccepted:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await registration_service.register(session, body, UserRole.FARMER, background_tasks)


@router.post("/register/buyer", response_model=RegistrationAccepted, status_code=202)
@limiter.limit("10/hour")
async def register_buyer(
    request: Request,
    response: Response,
    body: RegisterRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> RegistrationAccepted:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await registration_service.register(session, body, UserRole.BUYER, background_tasks)


@router.post("/register/verify", response_model=AuthResult, response_model_exclude_none=True, status_code=201)
@limiter.limit("5/minute")
async def verify_registration(
    request: Request, response: Response, body: RegisterVerifyRequest, session: AsyncSession = Depends(get_db)
) -> AuthResult:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await registration_service.verify(session, body.registration_token, body.code)


@router.post("/register/resend", response_model=ResendAccepted, status_code=202)
@limiter.limit("5/hour")
async def resend_registration_code(
    request: Request,
    body: RegisterResendRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> ResendAccepted:
    del request
    return await registration_service.resend(session, body.registration_token, background_tasks)


# Sign in ---------------------------------------------------------------------------------------


@router.post("/login", response_model=AuthResult, response_model_exclude_none=True)
@limiter.limit("5/minute")
async def login(
    request: Request, response: Response, body: LoginRequest, session: AsyncSession = Depends(get_db)
) -> AuthResult:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await auth_service.login(session, body.email, body.password)


@router.post("/login/verify", response_model=AuthResult, response_model_exclude_none=True)
@limiter.limit("10/minute")
async def verify_login(
    request: Request, response: Response, body: LoginVerifyRequest, session: AsyncSession = Depends(get_db)
) -> AuthResult:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await auth_service.verify_login(session, body.challenge_token, body.code)


@router.post("/login/resend", response_model=LoginResendOut)
@limiter.limit("5/hour")
async def resend_login_code(
    request: Request, body: LoginResendRequest, session: AsyncSession = Depends(get_db)
) -> LoginResendOut:
    del request
    return await auth_service.resend_login_code(session, body.challenge_token)


@router.post("/change-password", response_model=PasswordChangedResponse)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user_any),
    session: AsyncSession = Depends(get_db),
) -> PasswordChangedResponse:
    await auth_service.change_password(session, user, body.current_password, body.new_password)
    return PasswordChangedResponse(message=PASSWORD_CHANGED, login_required=True)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user_any), session: AsyncSession = Depends(get_db)) -> UserOut:
    google_linked = await google_auth_service.is_google_linked(session, user.id)
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        role=str(getattr(user.role, "value", user.role)),
        is_super_admin=user.is_super_admin,
        must_change_password=user.must_change_password,
        password_set=auth_service.password_is_set(user),
        google_linked=google_linked,
        created_at=user.created_at,
    )


# Google sign in (farmers and buyers; see app/services/google_auth_service.py) --------------------


@router.post("/google/nonce", response_model=GoogleNonceOut)
@limiter.limit("20/minute")
async def google_nonce(
    request: Request, response: Response, session: AsyncSession = Depends(get_db)
) -> GoogleNonceOut:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await google_auth_service.issue_nonce(session)


@router.post("/google", response_model=GoogleAuthResult, response_model_exclude_none=True)
@limiter.limit("10/minute")
async def google_sign_in(
    request: Request,
    response: Response,
    body: GoogleSignInRequest,
    session: AsyncSession = Depends(get_db),
    verifier: GoogleVerifier = Depends(get_google_verifier),
) -> GoogleAuthResult:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await google_auth_service.sign_in(session, verifier, body.credential, body.nonce)


@router.post("/google/complete", response_model=AuthResult, response_model_exclude_none=True, status_code=201)
@limiter.limit("10/minute")
async def google_complete(
    request: Request, response: Response, body: GoogleSignupRequest, session: AsyncSession = Depends(get_db)
) -> AuthResult:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await google_auth_service.complete(session, body.signup_token, body.role)


# Invitations and password reset ----------------------------------------------------------------


@router.post("/accept-invite", response_model=AuthResult, response_model_exclude_none=True, status_code=201)
@limiter.limit("5/minute")
async def accept_invite(
    request: Request, response: Response, body: AcceptInviteRequest, session: AsyncSession = Depends(get_db)
) -> AuthResult:
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


@router.post("/password-reset/request", response_model=AcceptedOut, status_code=202)
@limiter.limit("3/hour")
async def request_password_reset(
    request: Request,
    body: EmailRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> AcceptedOut:
    del request
    await password_reset_service.request_reset(session, body.email, background_tasks)
    return AcceptedOut(message=RESET_CODE_REQUESTED)


@router.post("/password-reset/confirm", status_code=204, response_class=Response)
@limiter.limit("5/minute")
async def confirm_password_reset(
    request: Request, body: PasswordResetConfirmRequest, session: AsyncSession = Depends(get_db)
) -> Response:
    del request
    await password_reset_service.confirm_reset(session, body)
    return Response(status_code=204)
