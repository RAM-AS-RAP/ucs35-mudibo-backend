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
