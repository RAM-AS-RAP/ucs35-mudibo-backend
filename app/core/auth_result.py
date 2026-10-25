"""``AuthResult``: the one response shape of every sign in endpoint.

``status`` is ``signed_in`` (an access token follows) or ``code_required`` (an emailed code is the
next step). Fields that do not apply to the status are omitted from the JSON: the routes use
``response_model_exclude_none=True`` and the builders below leave them ``None``.

``is_super_admin`` and ``must_change_password`` come from the users row, for the client UI only; the
access token never carries them.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.security import create_access_token

SIGNED_IN = "signed_in"
CODE_REQUIRED = "code_required"


class AuthResult(BaseModel):
    status: Literal["signed_in", "code_required"]
    # signed_in
    access_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    role: str | None = None
    must_change_password: bool | None = None
    is_super_admin: bool | None = None
    # code_required
    challenge_token: str | None = None
    email_masked: str | None = None
    code_expires_at: datetime | None = None
    resend_available_in: int | None = None
    code_sent: bool | None = None


def _field(user: Any, name: str) -> Any:
    if isinstance(user, Mapping):
        return user[name]
    return getattr(user, name)


def signed_in_result(user: Any) -> AuthResult:
    """Issue an access token for ``user`` (an ORM row or a mapping with id, role and the two flags)."""
    role = _field(user, "role")
    role_value = str(getattr(role, "value", role))
    return AuthResult(
        status=SIGNED_IN,
        access_token=create_access_token(_field(user, "id"), role_value),
        token_type="bearer",
        expires_in=get_settings().access_token_expire_minutes * 60,
        role=role_value,
        must_change_password=bool(_field(user, "must_change_password")),
        is_super_admin=bool(_field(user, "is_super_admin")),
    )


def code_required_result(
    *, challenge_token: str, email_masked: str, code_expires_at: datetime, resend_available_in: int, code_sent: bool
) -> AuthResult:
    return AuthResult(
        status=CODE_REQUIRED,
        challenge_token=challenge_token,
        email_masked=email_masked,
        code_expires_at=code_expires_at,
        resend_available_in=max(0, int(resend_available_in)),
        code_sent=code_sent,
    )
