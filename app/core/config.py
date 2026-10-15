"""Validated application settings."""

import re
from decimal import Decimal
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_URL_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_PHONE = re.compile(r"254[17][0-9]{8}")
_HOST_NAME = re.compile(r"(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?")


def split_csv(value: str) -> list[str]:
    """Split a comma separated setting into trimmed, non-empty items."""
    return [item.strip() for item in value.split(",") if item.strip()]


def origin_problem(origin: str) -> str | None:
    """Return why ``origin`` is not a bare http(s) origin (scheme, host, optional port), or None."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must start with http:// or https:// and name a host"
    if parts.username is not None or parts.password is not None:
        return "must not contain credentials"
    if parts.path or parts.query or parts.fragment or origin.endswith(("?", "#")):
        return "must not contain a path, query or fragment (no trailing slash)"
    try:
        parts.port
    except ValueError:
        return "has an invalid port"
    return None


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
    invite_code_pepper: str = ""
    invite_ttl_days: int = 7
    invite_code_ttl_minutes: int = 15
    invite_max_attempts: int = 10
    invite_max_sends: int = 5
    invite_resend_cooldown_seconds: int = 60
    invite_max_pending: int = 10
    reset_code_ttl_minutes: int = 15
    reset_max_attempts: int = 5
    reset_max_requests_per_hour: int = 3
    email_provider: Literal["console", "smtp", "resend", "brevo"] = "console"
    email_api_key: str = ""
    email_from_name: str = "Admin via AgriSence"
    email_from_address: str = ""
    email_reply_to: str = ""
    frontend_base_url: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl"] = "starttls"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: int = 15
    log_format: Literal["json", "text"] = "text"
    max_json_body_bytes: int = 65_536
    db_statement_timeout_ms: int = 15_000
    db_lock_timeout_ms: int = 5_000
    # Empty means "*" outside production; production requires explicit host names.
    allowed_hosts: str = ""
    # A development and test aid only; refused when APP_ENV is production.
    rate_limits_disabled: bool = False

    @field_validator("log_format", mode="before")
    @classmethod
    def normalize_log_format(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("max_json_body_bytes")
    @classmethod
    def validate_max_json_body_bytes(cls, value: int) -> int:
        if not 1024 <= value <= 10_485_760:
            raise ValueError("MAX_JSON_BODY_BYTES must be between 1024 and 10485760")
        return value

    @field_validator("db_statement_timeout_ms", "db_lock_timeout_ms")
    @classmethod
    def validate_db_timeouts(cls, value: int) -> int:
        # 0 disables the timeout; otherwise between 100 ms and 10 minutes.
        if value != 0 and not 100 <= value <= 600_000:
            raise ValueError("DB_STATEMENT_TIMEOUT_MS and DB_LOCK_TIMEOUT_MS must be 0 or between 100 and 600000")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, value: str) -> str:
        for host in split_csv(value):
            if host != "*" and not _HOST_NAME.fullmatch(host):
                raise ValueError("ALLOWED_HOSTS must be comma separated host names (no scheme or path)")
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        for origin in split_csv(value):
            if origin == "*":
                continue  # rejected in production by validate_production_edges
            problem = origin_problem(origin)
            if problem is not None:
                raise ValueError(f"Each CORS_ORIGINS entry {problem}")
        return value

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

    @field_validator("resolve_note_min_length")
    @classmethod
    def validate_resolve_note_min_length(cls, value: int) -> int:
        if not 1 <= value <= 500:
            raise ValueError("RESOLVE_NOTE_MIN_LENGTH must be between 1 and 500")
        return value

    @field_validator("invite_code_pepper")
    @classmethod
    def validate_invite_code_pepper(cls, value: str) -> str:
        # Empty is allowed so the rest of the API starts; invite and reset routes then return 503.
        if value and len(value) < 32:
            raise ValueError("INVITE_CODE_PEPPER must contain at least 32 characters")
        return value

    @field_validator("invite_ttl_days")
    @classmethod
    def validate_invite_ttl_days(cls, value: int) -> int:
        if not 1 <= value <= 30:
            raise ValueError("INVITE_TTL_DAYS must be between 1 and 30")
        return value

    @field_validator("invite_code_ttl_minutes", "reset_code_ttl_minutes")
    @classmethod
    def validate_code_ttl_minutes(cls, value: int) -> int:
        if not 5 <= value <= 60:
            raise ValueError("Code lifetimes must be between 5 and 60 minutes")
        return value

    @field_validator("invite_max_attempts", "reset_max_attempts")
    @classmethod
    def validate_code_max_attempts(cls, value: int) -> int:
        if not 1 <= value <= 20:
            raise ValueError("Code attempt limits must be between 1 and 20")
        return value

    @field_validator("invite_max_sends")
    @classmethod
    def validate_invite_max_sends(cls, value: int) -> int:
        # The admin_invites check constraint allows send_count between 1 and 5.
        if not 1 <= value <= 5:
            raise ValueError("INVITE_MAX_SENDS must be between 1 and 5")
        return value

    @field_validator("invite_resend_cooldown_seconds")
    @classmethod
    def validate_invite_resend_cooldown(cls, value: int) -> int:
        if not 10 <= value <= 3600:
            raise ValueError("INVITE_RESEND_COOLDOWN_SECONDS must be between 10 and 3600")
        return value

    @field_validator("invite_max_pending")
    @classmethod
    def validate_invite_max_pending(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("INVITE_MAX_PENDING must be between 1 and 100")
        return value

    @field_validator("reset_max_requests_per_hour")
    @classmethod
    def validate_reset_max_requests_per_hour(cls, value: int) -> int:
        if not 1 <= value <= 20:
            raise ValueError("RESET_MAX_REQUESTS_PER_HOUR must be between 1 and 20")
        return value

    @field_validator("smtp_port")
    @classmethod
    def validate_smtp_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        return value

    @field_validator("smtp_timeout_seconds")
    @classmethod
    def validate_smtp_timeout(cls, value: int) -> int:
        if not 1 <= value <= 120:
            raise ValueError("SMTP_TIMEOUT_SECONDS must be between 1 and 120")
        return value

    @field_validator("email_from_name", "email_from_address", "email_reply_to", "smtp_username")
    @classmethod
    def reject_header_line_breaks(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Email header settings must not contain line breaks")
        return value.strip()

    @model_validator(mode="after")
    def validate_email_settings(self) -> "Settings":
        production = self.app_env.strip().lower() == "production"
        if production and self.email_provider == "console":
            raise ValueError("EMAIL_PROVIDER=console is not allowed when APP_ENV is production")
        if self.email_provider in ("resend", "brevo") and not (self.email_api_key and self.email_from_address):
            raise ValueError("EMAIL_PROVIDER resend and brevo require EMAIL_API_KEY and EMAIL_FROM_ADDRESS")
        if self.email_provider == "smtp" and not all(
            (self.smtp_host, self.smtp_port, self.smtp_username, self.smtp_password, self.email_from_address)
        ):
            raise ValueError(
                "EMAIL_PROVIDER smtp requires SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and EMAIL_FROM_ADDRESS"
            )
        if production and not self.frontend_base_url.startswith("https://"):
            raise ValueError("FRONTEND_BASE_URL must start with https:// when APP_ENV is production")
        return self

    @model_validator(mode="after")
    def validate_production_edges(self) -> "Settings":
        if not self.is_production:
            return self
        if "*" in split_csv(self.cors_origins):
            raise ValueError("CORS_ORIGINS must not contain * when APP_ENV is production")
        hosts = split_csv(self.allowed_hosts)
        if not hosts or any("*" in host for host in hosts):
            raise ValueError("ALLOWED_HOSTS must list explicit host names (no *) when APP_ENV is production")
        if self.rate_limits_disabled:
            raise ValueError("RATE_LIMITS_DISABLED is not allowed when APP_ENV is production")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @property
    def allowed_host_list(self) -> list[str]:
        """Hosts for TrustedHostMiddleware; ``["*"]`` when unset outside production."""
        return split_csv(self.allowed_hosts) or ["*"]

    @property
    def codes_configured(self) -> bool:
        """Report whether invite and reset codes can be hashed (the pepper is set)."""
        return bool(self.invite_code_pepper)

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
        return split_csv(self.cors_origins)


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
