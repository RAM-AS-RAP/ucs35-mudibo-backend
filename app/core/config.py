"""Validated application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = ""
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "listing-images"
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    cors_origins: str = ""
    login_max_failures: int = 5
    login_failure_window_minutes: int = 15
    mpesa_env: str = "sandbox"
    mpesa_base_url: str = "https://sandbox.safaricom.co.ke"
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_shortcode: str = "174379"
    mpesa_passkey: str = ""
    mpesa_callback_base_url: str = ""
    mpesa_callback_token: str = ""
    mpesa_stk_target: Literal["registered", "test_msisdn"] = "registered"
    mpesa_test_msisdn: str = "254708374149"
    order_expiry_minutes: int = 5
    reconcile_after_seconds: int = 90
    max_image_bytes: int = 5_242_880
    max_active_listings_per_farmer: int = 50
    image_max_dimension: int = 1600
    image_jpeg_quality: int = 85
    seed_admin_name: str = ""
    seed_admin_phone: str = ""
    seed_admin_email: str = ""
    seed_admin_password: str = ""

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        return value

    @field_validator("access_token_expire_minutes")
    @classmethod
    def validate_access_token_expiry(cls, value: int) -> int:
        if not 5 <= value <= 1440:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be between 5 and 1440")
        return value

    @field_validator("login_max_failures", "login_failure_window_minutes")
    @classmethod
    def validate_positive_login_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Login throttle settings must be positive")
        return value

    @field_validator("max_active_listings_per_farmer", "image_max_dimension")
    @classmethod
    def validate_positive_farmer_image_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Farmer listing and image dimensions must be positive")
        return value

    @field_validator("image_jpeg_quality")
    @classmethod
    def validate_image_jpeg_quality(cls, value: int) -> int:
        if not 1 <= value <= 95:
            raise ValueError("IMAGE_JPEG_QUALITY must be between 1 and 95")
        return value

    @field_validator("mpesa_env")
    @classmethod
    def sandbox_only(cls, value: str) -> str:
        if value != "sandbox":
            raise ValueError("Only the Daraja sandbox is supported")
        return value

    @field_validator("mpesa_base_url")
    @classmethod
    def sandbox_url_only(cls, value: str) -> str:
        if value != "https://sandbox.safaricom.co.ke":
            raise ValueError("MPESA_BASE_URL must be the Daraja sandbox URL")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        """Return configured CORS origins as trimmed values."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
