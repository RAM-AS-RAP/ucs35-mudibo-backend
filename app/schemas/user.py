"""User-facing account schemas. Administrator user schemas live in ``app.schemas.admin``."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    phone_number: str
    email: EmailStr | None
    role: str
    must_change_password: bool
    created_at: datetime
