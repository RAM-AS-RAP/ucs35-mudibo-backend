"""Invite, password reset and email check schemas.

No schema here carries a code, a code hash or a password hash. Request bodies reject unknown fields,
so a client can never send ``role``, ``is_super_admin`` or any other flag.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.common import Page
from app.utils.phone import normalize_phone

CODE_MIN_LENGTH = 4
CODE_MAX_LENGTH = 20
# The policy (app.core.password_policy) enforces the real 128 limit with a password_too_weak reason.
PASSWORD_FIELD_MAX_LENGTH = 1024


def _lower_email(value: object) -> str:
    return str(value).strip().lower()


class CreateInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=100)
    phone_number: str
    email: EmailStr

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 2:
            raise ValueError("Name must contain at least 2 characters")
        return stripped

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("email")
    @classmethod
    def lower_email(cls, value: str) -> str:
        return _lower_email(value)


class InviteSentOut(BaseModel):
    id: UUID
    name: str
    email_masked: str
    phone_masked: str
    expires_at: datetime
    code_expires_at: datetime
    email_sent: bool
    message: str


class InviteStatus(StrEnum):
    PENDING = "pending"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"
    LOCKED = "locked"


class InviteOut(BaseModel):
    id: UUID
    name: str
    email_masked: str
    status: InviteStatus
    created_by_name: str | None
    created_at: datetime
    expires_at: datetime
    send_count: int
    attempts: int
    used_at: datetime | None
    revoked_at: datetime | None


class InvitePage(Page[InviteOut]):
    pass


class EmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email")
    @classmethod
    def lower_email(cls, value: str) -> str:
        return _lower_email(value)


class AcceptInviteRequest(EmailRequest):
    code: str = Field(min_length=CODE_MIN_LENGTH, max_length=CODE_MAX_LENGTH)
    password: str = Field(min_length=1, max_length=PASSWORD_FIELD_MAX_LENGTH)


class PasswordResetConfirmRequest(EmailRequest):
    code: str = Field(min_length=CODE_MIN_LENGTH, max_length=CODE_MAX_LENGTH)
    new_password: str = Field(min_length=1, max_length=PASSWORD_FIELD_MAX_LENGTH)


class AcceptedOut(BaseModel):
    """The identical 202 body of the public code request routes."""

    message: str


class EmailCheckOut(BaseModel):
    email_sent: bool
