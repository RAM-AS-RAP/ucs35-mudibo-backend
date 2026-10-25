"""Configuration validation and CORS parsing tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture
def valid_settings() -> dict[str, object]:
    # `_env_file: None` keeps the developer's real .env out of these tests: its values must not
    # change the defaults under test, and must never appear in a failing assertion's output.
    return {
        "_env_file": None,
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


def test_payment_settings_have_documented_defaults(valid_settings) -> None:
    settings = Settings(**valid_settings)
    assert settings.mpesa_max_attempts_per_order == 3
    assert settings.mpesa_min_seconds_before_expiry == 90
    assert settings.mpesa_stale_initiating_seconds == 60
    assert settings.mpesa_phone_attempts_per_hour == 5
    assert settings.mpesa_buyer_attempts_per_hour == 10
    assert settings.mpesa_buyer_distinct_phones_per_day == 3
    assert settings.mpesa_stk_target == "entered"
    assert settings.mpesa_query_min_interval_seconds == 20
    assert settings.mpesa_timeout_reconcile_window_minutes == 15
    assert settings.callback_max_bytes == 16384
    assert settings.mpesa_transaction_desc == "Crop order"


def test_empty_payment_settings_do_not_fail_startup(valid_settings) -> None:
    settings = Settings(
        **valid_settings,
        mpesa_consumer_key="",
        mpesa_consumer_secret="",
        mpesa_passkey="",
        mpesa_callback_token="",
        mpesa_callback_base_url="",
    )
    assert settings.payments_configured is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mpesa_callback_token", "too-short-token"),
        ("mpesa_callback_token", "not/url/safe/but/long/enough/token"),
        ("mpesa_callback_token", "has spaces but is long enough to pass"),
        ("mpesa_test_msisdn", "0708374149"),
        ("mpesa_test_msisdn", "254608374149"),
        ("mpesa_transaction_desc", "A description that is too long"),
        ("mpesa_transaction_desc", ""),
        ("mpesa_max_attempts_per_order", 0),
        ("callback_max_bytes", 0),
        ("mpesa_buyer_attempts_per_hour", 0),
        ("mpesa_buyer_attempts_per_hour", 101),
        ("mpesa_buyer_distinct_phones_per_day", 0),
        ("mpesa_buyer_distinct_phones_per_day", 21),
        ("mpesa_stk_target", "buyer"),
        ("mpesa_stk_target", ""),
    ],
)
def test_payment_settings_are_validated(valid_settings, field, value) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**valid_settings, field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mpesa_buyer_attempts_per_hour", 1),
        ("mpesa_buyer_attempts_per_hour", 100),
        ("mpesa_buyer_distinct_phones_per_day", 1),
        ("mpesa_buyer_distinct_phones_per_day", 20),
    ],
)
def test_buyer_payment_limits_accept_their_range(valid_settings, field, value) -> None:
    assert getattr(Settings(**{**valid_settings, field: value}), field) == value


@pytest.mark.parametrize("target", ["entered", "registered", "test_msisdn"])
def test_every_stk_target_is_accepted(valid_settings, target) -> None:
    assert Settings(**{**valid_settings, "mpesa_stk_target": target}).mpesa_stk_target == target


def test_registered_target_logs_one_startup_warning(valid_settings, caplog) -> None:
    from app.services import payment_service

    caplog.set_level("WARNING", logger=payment_service.logger.name)
    payment_service.log_startup_warnings(Settings(**{**valid_settings, "mpesa_stk_target": "registered"}))
    warnings = [record for record in caplog.records if "MPESA_STK_TARGET" in record.getMessage()]
    assert len(warnings) == 1
    assert "entered" in warnings[0].getMessage()

    caplog.clear()
    payment_service.log_startup_warnings(Settings(**{**valid_settings, "mpesa_stk_target": "entered"}))
    payment_service.log_startup_warnings(Settings(**{**valid_settings, "mpesa_stk_target": "test_msisdn"}))
    assert not [record for record in caplog.records if "MPESA_STK_TARGET" in record.getMessage()]


def test_payments_require_an_https_callback_base_url(valid_settings) -> None:
    configured = {
        **valid_settings,
        "mpesa_consumer_key": "key",
        "mpesa_consumer_secret": "secret",
        "mpesa_passkey": "passkey",
        "mpesa_callback_token": "a" * 24,
    }
    assert Settings(**configured, mpesa_callback_base_url="https://x.example").payments_configured
    assert not Settings(**configured, mpesa_callback_base_url="http://x.example").payments_configured


def test_admin_settings_have_documented_defaults(valid_settings) -> None:
    settings = Settings(**valid_settings)
    assert settings.resolve_note_min_length == 10
    assert not hasattr(settings, "admin_temp_password_bytes")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resolve_note_min_length", 0),
        ("resolve_note_min_length", 501),
    ],
)
def test_admin_settings_are_validated(valid_settings, field, value) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**valid_settings, field: value})


# Identity v2 ---------------------------------------------------------------------------------


def test_identity_settings_have_documented_defaults(valid_settings) -> None:
    settings = Settings(**valid_settings)
    assert settings.access_token_expire_minutes == 480
    assert settings.login_otp_role_set == {"farmer", "buyer", "admin"}
    assert (
        settings.reg_ttl_minutes,
        settings.reg_code_ttl_minutes,
        settings.reg_max_attempts,
        settings.reg_max_sends,
        settings.reg_resend_cooldown_seconds,
        settings.reg_exists_notice_minutes,
    ) == (60, 15, 5, 5, 60, 10)
    assert (
        settings.login_code_ttl_minutes,
        settings.login_challenge_ttl_minutes,
        settings.login_max_attempts,
        settings.login_max_sends,
        settings.login_resend_cooldown_seconds,
        settings.login_challenges_per_hour,
    ) == (10, 10, 5, 3, 60, 10)
    assert not hasattr(settings, "seed_admin_phone")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("login_otp_roles", "farmer,owner"),
        ("reg_ttl_minutes", 9),
        ("reg_ttl_minutes", 1441),
        ("reg_code_ttl_minutes", 4),
        ("reg_max_attempts", 0),
        ("reg_max_sends", 6),
        ("reg_resend_cooldown_seconds", 9),
        ("reg_exists_notice_minutes", 0),
        ("login_code_ttl_minutes", 61),
        ("login_challenge_ttl_minutes", 4),
        ("login_max_attempts", 21),
        ("login_max_sends", 0),
        ("login_resend_cooldown_seconds", 3601),
        ("login_challenges_per_hour", 0),
        ("login_challenges_per_hour", 101),
        ("access_token_expire_minutes", 1441),
    ],
)
def test_identity_settings_are_validated(valid_settings, field, value) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**valid_settings, field: value})


def test_a_code_cannot_outlive_its_registration_or_challenge(valid_settings) -> None:
    with pytest.raises(ValidationError):
        Settings(**valid_settings, reg_ttl_minutes=10, reg_code_ttl_minutes=15)
    with pytest.raises(ValidationError):
        Settings(**valid_settings, login_challenge_ttl_minutes=5, login_code_ttl_minutes=10)


def test_login_otp_roles_is_normalised(valid_settings) -> None:
    settings = Settings(**valid_settings, login_otp_roles=" Admin , farmer,admin ")
    assert settings.login_otp_roles == "admin,farmer"
    assert settings.login_otp_role_set == {"admin", "farmer"}
