"""User-facing and administrator user schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.utils.phone import normalize_phone


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    phone_number: str
    email: EmailStr | None
    role: str
    must_change_password: bool
    created_at: datetime


class UserRead(UserOut):
    """Extended account projection reserved for administrative listings."""

    is_disabled: bool


class AdminCreate(BaseModel):
    name: str
    phone_number: str
    email: EmailStr | None = None
    temporary_password: str = Field(min_length=8)

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)


class UserPage(BaseModel):
    items: list[UserRead]
    limit: int
    offset: int
