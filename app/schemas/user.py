"""User-facing account schemas. Administrator user schemas live in ``app.schemas.admin``."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    """``GET /auth/me``, the only response that uses this schema."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str
    role: str
    # Always read from the users row (false for everyone but the super admin); never from the token.
    is_super_admin: bool
    must_change_password: bool
    # False only for an account created with Google that has not set a password yet (Forgot password sets one).
    password_set: bool
    # True when a Google account is connected; the Google account id itself is never returned.
    google_linked: bool
    created_at: datetime
