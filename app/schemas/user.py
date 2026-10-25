"""User-facing account schemas. Administrator user schemas live in ``app.schemas.admin``."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str
    role: str
    # Always read from the users row (false for everyone but the super admin); never from the token.
    is_super_admin: bool
    must_change_password: bool
    created_at: datetime
