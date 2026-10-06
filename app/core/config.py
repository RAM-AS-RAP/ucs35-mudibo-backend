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
    seed_admin_name: str = ""
    seed_admin_phone: str = ""
    seed_admin_email: str = ""
    seed_admin_password: str = ""

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
