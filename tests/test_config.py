"""Configuration validation and CORS parsing tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture
def valid_settings() -> dict[str, object]:
    return {
        "jwt_secret": "test-only-jwt-secret-at-least-thirty-two-characters",
        "mpesa_env": "sandbox",
        "mpesa_base_url": "https://sandbox.safaricom.co.ke",
    }


def test_jwt_secret_requires_32_characters(valid_settings) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**valid_settings, "jwt_secret": "too-short"})


@pytest.mark.parametrize("minutes", [4, 1441])
def test_access_token_expiry_is_bounded(valid_settings, minutes) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**valid_settings, "access_token_expire_minutes": minutes})


def test_cors_origins_are_split_and_trimmed(valid_settings) -> None:
    settings = Settings(**{**valid_settings, "cors_origins": " https://pwa.example,https://admin.example , ,"})
    assert settings.cors_origin_list == ["https://pwa.example", "https://admin.example"]


def test_login_throttle_settings_are_available(valid_settings) -> None:
    settings = Settings(**valid_settings)
    assert settings.login_max_failures == 5
    assert settings.login_failure_window_minutes == 15


def test_farmer_listing_and_image_settings_have_documented_defaults(valid_settings) -> None:
    settings = Settings(**valid_settings)
    assert settings.max_active_listings_per_farmer == 50
    assert settings.image_max_dimension == 1600
    assert settings.image_jpeg_quality == 85


def test_order_settings_have_documented_defaults(valid_settings) -> None:
    settings = Settings(**valid_settings)
    assert settings.min_order_quantity == 1
    assert settings.min_order_total_kes == 10
    assert settings.max_pending_orders_per_buyer == 3
    assert settings.order_reference_length == 8
    assert settings.order_expiry_minutes == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_reference_length", 5),
        ("order_reference_length", 13),
        ("order_expiry_minutes", 1),
        ("order_expiry_minutes", 31),
        ("min_order_quantity", 0),
        ("max_pending_orders_per_buyer", 0),
    ],
)
def test_order_settings_are_validated_at_startup(valid_settings, field, value) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**valid_settings, field: value})
