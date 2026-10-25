"""Authentication request and response schemas.

Every request body rejects unknown fields, so a client can never send ``role``, ``is_super_admin`` or
any other flag. The role of a registration comes only from the route. Sign in responses use
``app.core.auth_result.AuthResult``.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

CODE_MIN_LENGTH = 4
CODE_MAX_LENGTH = 20
# Tokens are secrets.token_urlsafe(32) (43 characters); the bounds only reject obvious garbage.
TOKEN_MIN_LENGTH = 16
TOKEN_MAX_LENGTH = 128


def validate_password_policy(value: object) -> object:
    """Require 8-128 characters with uppercase, lowercase, digit, and symbol."""
    if not isinstance(value, str):
        return value

    missing: list[str] = []
    if len(value) < 8:
        missing.append("at least 8 characters")
    if len(value) > 128:
        missing.append("no more than 128 characters")
    if not any(character.isupper() for character in value):
        missing.append("an uppercase letter")
    if not any(character.islower() for character in value):
        missing.append("a lowercase letter")
    if not any(character.isdecimal() for character in value):
        missing.append("a digit")
    if not any(not character.isalnum() and not character.isspace() for character in value):
        missing.append("a symbol")
    if missing:
        raise ValueError(f"Password must contain {'; '.join(missing)}")
    return value


def _lower_email(value: object) -> str:
    return str(value).strip().lower()


class RegisterRequest(BaseModel):
    """Farmer or buyer registration; the account is created only after the emailed code is entered."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password", mode="before")
    @classmethod
    def enforce_password_policy(cls, value: object) -> object:
        return validate_password_policy(value)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return _lower_email(value)


class RegistrationAccepted(BaseModel):
    """The identical 202 body of every registration request, whether or not the address has an account."""

    message: str
    registration_token: str
    email_masked: str
    code_expires_at: datetime
    resend_available_in: int


class RegisterVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_token: str = Field(min_length=TOKEN_MIN_LENGTH, max_length=TOKEN_MAX_LENGTH)
    code: str = Field(min_length=CODE_MIN_LENGTH, max_length=CODE_MAX_LENGTH)


class RegisterResendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_token: str = Field(min_length=TOKEN_MIN_LENGTH, max_length=TOKEN_MAX_LENGTH)


class ResendAccepted(BaseModel):
    """The identical 202 body of every registration resend request."""

    message: str
    resend_available_in: int


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return _lower_email(value)


class LoginVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str = Field(min_length=TOKEN_MIN_LENGTH, max_length=TOKEN_MAX_LENGTH)
    code: str = Field(min_length=CODE_MIN_LENGTH, max_length=CODE_MAX_LENGTH)


class LoginResendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str = Field(min_length=TOKEN_MIN_LENGTH, max_length=TOKEN_MAX_LENGTH)


class LoginResendOut(BaseModel):
    code_sent: bool
    resend_available_in: int
    code_expires_at: datetime


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password", mode="before")
    @classmethod
    def enforce_new_password_policy(cls, value: object) -> object:
        return validate_password_policy(value)

    @model_validator(mode="after")
    def reject_unchanged_password(self) -> "ChangePasswordRequest":
        if self.new_password == self.current_password:
            raise ValueError("New password must differ from current password")
        return self


class PasswordChangedResponse(BaseModel):
    """After a password change every earlier access token, including the current one, is revoked."""

    message: str
    login_required: bool = True
