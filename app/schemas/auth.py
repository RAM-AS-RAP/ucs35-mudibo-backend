"""Authentication request and response schemas."""

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.utils.phone import normalize_phone


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


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=100)
    phone_number: str
    email: EmailStr | None = None
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password", mode="before")
    @classmethod
    def enforce_password_policy(cls, value: object) -> object:
        return validate_password_policy(value)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr | None) -> str | None:
        return str(value).lower() if value is not None else None


# Compatibility name for other modules that imported the original schema.
RegistrationRequest = RegisterRequest


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    # From the users row at login, for the client UI only; the JWT never carries it.
    is_super_admin: bool
    must_change_password: bool
