"""Super administrator routes: admin invitations and the email delivery check.

Each route requires the super admin (role and flag loaded from the database on every request) and is
rate limited per IP. Bodies reject unknown fields, so no role or flag can be sent. Responses never
contain a code, a code hash or a password.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_super_admin
from app.core.errors import AppError
from app.db import get_db
from app.integrations import email as email_integration
from app.integrations.email_templates import check_email
from app.models.user import User
from app.routers.admin_users import require_empty_body
from app.routers.auth import limiter
from app.schemas.common import Pagination, pagination_params
from app.schemas.invite import CreateInviteRequest, EmailCheckOut, InvitePage, InviteSentOut, InviteStatus
from app.services import invite_service

router = APIRouter()

NO_STORE = "no-store"


@router.post("/invites", status_code=201, response_model=InviteSentOut)
@limiter.limit("10/hour")
async def create_invite(
    request: Request,
    response: Response,
    body: CreateInviteRequest,
    actor: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db),
) -> InviteSentOut:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await invite_service.create_invite(session, actor, body)


@router.post("/invites/{invite_id}/resend", response_model=InviteSentOut, dependencies=[Depends(require_empty_body)])
@limiter.limit("10/hour")
async def resend_invite(
    request: Request,
    response: Response,
    invite_id: UUID,
    actor: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db),
) -> InviteSentOut:
    del request
    response.headers["Cache-Control"] = NO_STORE
    return await invite_service.resend_invite(session, actor, invite_id)


@router.get("/invites", response_model=InvitePage)
@limiter.limit("10/hour")
async def list_invites(
    request: Request,
    status_filter: InviteStatus | None = Query(default=None, alias="status"),
    pagination: Pagination = Depends(pagination_params),
    actor: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db),
) -> InvitePage:
    del request, actor
    value = status_filter.value if status_filter is not None else None
    return await invite_service.list_invites(session, pagination, value)


@router.delete("/invites/{invite_id}", status_code=204, response_class=Response)
@limiter.limit("10/hour")
async def revoke_invite(
    request: Request,
    invite_id: UUID,
    actor: User = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db),
) -> Response:
    del request
    await invite_service.revoke_invite(session, actor, invite_id)
    return Response(status_code=204)


@router.post("/email/test", response_model=EmailCheckOut, dependencies=[Depends(require_empty_body)])
@limiter.limit("5/hour")
async def send_check_email(request: Request, actor: User = Depends(require_super_admin)) -> EmailCheckOut:
    """Send a short message to the acting super admin's own address; it carries no secret and no code."""
    del request
    if not actor.email:
        raise AppError(status.HTTP_409_CONFLICT, "no_email", "Your account has no email address.")
    try:
        await email_integration.send_email(check_email(actor.email))
    except email_integration.EmailDeliveryError as exc:
        # Only the category is returned, never the provider's or the library's text.
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            "email_failed",
            "The email could not be sent.",
            extra={"category": exc.category},
        ) from None
    return EmailCheckOut(email_sent=True)
