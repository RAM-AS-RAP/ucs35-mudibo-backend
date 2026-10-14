"""Validated application settings."""

import re
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_URL_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_PHONE = re.compile(r"254[17][0-9]{8}")


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
    mpesa_max_attempts_per_order: int = 3
    mpesa_min_seconds_before_expiry: int = 90
    mpesa_stale_initiating_seconds: int = 60
    mpesa_phone_attempts_per_hour: int = 5
    mpesa_query_min_interval_seconds: int = 20
    mpesa_timeout_reconcile_window_minutes: int = 15
    mpesa_transaction_desc: str = "Crop order"
    callback_max_bytes: int = 16384
    admin_temp_password_bytes: int = 12
    resolve_note_min_length: int = 10
    order_expiry_minutes: int = 5
    reconcile_after_seconds: int = 90
    max_image_bytes: int = 5_242_880
    max_active_listings_per_farmer: int = 50
    image_max_dimension: int = 1600
    image_jpeg_quality: int = 85
    min_order_quantity: Decimal = Decimal("1")
    min_order_total_kes: int = 10
    max_pending_orders_per_buyer: int = 3
    order_reference_length: int = 8
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

    @field_validator("order_expiry_minutes")
    @classmethod
    def validate_order_expiry(cls, value: int) -> int:
        if not 2 <= value <= 30:
            raise ValueError("ORDER_EXPIRY_MINUTES must be between 2 and 30")
        return value

    @field_validator("order_reference_length")
    @classmethod
    def validate_order_reference_length(cls, value: int) -> int:
        if not 6 <= value <= 12:
            raise ValueError("ORDER_REFERENCE_LENGTH must be between 6 and 12")
        return value

    @field_validator("min_order_quantity")
    @classmethod
    def validate_min_order_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("MIN_ORDER_QUANTITY must be greater than zero")
        return value

    @field_validator("min_order_total_kes", "max_pending_orders_per_buyer")
    @classmethod
    def validate_positive_order_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Order limits must be positive")
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

    @field_validator("mpesa_callback_token")
    @classmethod
    def validate_callback_token(cls, value: str) -> str:
        # Empty is allowed so non-payment routes and tests still start; payment routes then return 503.
        if value and (len(value) < 24 or not _URL_SAFE_TOKEN.fullmatch(value)):
            raise ValueError("MPESA_CALLBACK_TOKEN must be at least 24 URL-safe characters (A-Z, a-z, 0-9, _ and -)")
        return value

    @field_validator("mpesa_test_msisdn")
    @classmethod
    def validate_test_msisdn(cls, value: str) -> str:
        if value and not _PHONE.fullmatch(value):
            raise ValueError("MPESA_TEST_MSISDN must match 254[17]XXXXXXXX")
        return value

    @field_validator("mpesa_transaction_desc")
    @classmethod
    def validate_transaction_desc(cls, value: str) -> str:
        if not 1 <= len(value) <= 13:
            raise ValueError("MPESA_TRANSACTION_DESC must be 1 to 13 characters")
        return value

    @field_validator(
        "mpesa_max_attempts_per_order",
        "mpesa_min_seconds_before_expiry",
        "mpesa_stale_initiating_seconds",
        "mpesa_phone_attempts_per_hour",
        "mpesa_query_min_interval_seconds",
        "mpesa_timeout_reconcile_window_minutes",
        "callback_max_bytes",
        "reconcile_after_seconds",
    )
    @classmethod
    def validate_positive_payment_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Payment limits must be positive")
        return value

    @field_validator("admin_temp_password_bytes")
    @classmethod
    def validate_admin_temp_password_bytes(cls, value: int) -> int:
        if not 12 <= value <= 32:
            raise ValueError("ADMIN_TEMP_PASSWORD_BYTES must be between 12 and 32")
        return value

    @field_validator("resolve_note_min_length")
    @classmethod
    def validate_resolve_note_min_length(cls, value: int) -> int:
        if not 1 <= value <= 500:
            raise ValueError("RESOLVE_NOTE_MIN_LENGTH must be between 1 and 500")
        return value

    @property
    def payments_configured(self) -> bool:
        """Report whether every setting a payment endpoint needs is present and usable."""
        required = (
            self.mpesa_consumer_key,
            self.mpesa_consumer_secret,
            self.mpesa_passkey,
            self.mpesa_callback_token,
            self.mpesa_shortcode,
        )
        if not all(required) or not self.mpesa_callback_base_url.startswith("https://"):
            return False
        return self.mpesa_stk_target != "test_msisdn" or bool(self.mpesa_test_msisdn)

    @property
    def cors_origin_list(self) -> list[str]:
        """Return configured CORS origins as trimmed values."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
