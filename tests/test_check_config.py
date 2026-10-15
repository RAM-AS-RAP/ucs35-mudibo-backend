"""``scripts/check_config.py`` classifies settings and never prints a value."""

import io

import pytest

from scripts import check_config
from scripts.check_config import INVALID, MISSING, OK, WEAK, main, run_checks

JWT = "j" * 64
PEPPER = "p" * 48
CALLBACK = "c" * 40
DB_PASSWORD = "db-password-value-77"
API_KEY = "email-api-key-value-88"


def valid_production() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "DATABASE_URL": f"postgresql://postgres.ref:{DB_PASSWORD}@aws-0-eu.pooler.supabase.com:6543/postgres",
        "JWT_SECRET": JWT,
        "CORS_ORIGINS": "https://app.agrisence.test",
        "ALLOWED_HOSTS": "api.agrisence.test",
        "SUPABASE_URL": "https://ref.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-value-99",
        "MPESA_ENV": "sandbox",
        "MPESA_BASE_URL": "https://sandbox.safaricom.co.ke",
        "MPESA_SHORTCODE": "174379",
        "MPESA_CONSUMER_KEY": "consumer-key-value",
        "MPESA_CONSUMER_SECRET": "consumer-secret-value",
        "MPESA_PASSKEY": "passkey-value",
        "MPESA_CALLBACK_BASE_URL": "https://api.agrisence.test",
        "MPESA_CALLBACK_TOKEN": CALLBACK,
        "EMAIL_PROVIDER": "resend",
        "EMAIL_API_KEY": API_KEY,
        "EMAIL_FROM_ADDRESS": "noreply@agrisence.test",
        "INVITE_CODE_PEPPER": PEPPER,
        "FRONTEND_BASE_URL": "https://app.agrisence.test",
    }


def status_of(env: dict[str, str], setting: str) -> str:
    return next(check.status for check in run_checks(env) if check.item == setting)


def test_a_valid_production_environment_passes() -> None:
    env = valid_production()
    assert all(check.status == OK for check in run_checks(env)), [c for c in run_checks(env) if c.status != OK]
    out = io.StringIO()
    assert main(env, env_file=None, out=out) == 0
    assert out.getvalue().rstrip().splitlines()[-1] == "Environment: production"


@pytest.mark.parametrize(
    ("setting", "value", "expected"),
    [
        ("DATABASE_URL", "", MISSING),
        ("DATABASE_URL", "mysql://u:p@db.example:6543/x", INVALID),
        ("DATABASE_URL", "postgresql://u:p@db.example.supabase.com:5432/postgres", WEAK),
        ("DATABASE_URL", "postgresql://u:p@127.0.0.1:54322/postgres", INVALID),
        ("JWT_SECRET", "", MISSING),
        ("JWT_SECRET", "short", INVALID),
        ("JWT_SECRET", "x" * 40, WEAK),
        ("CORS_ORIGINS", "", MISSING),
        ("CORS_ORIGINS", "*", INVALID),
        ("CORS_ORIGINS", "https://app.agrisence.test/", INVALID),
        ("CORS_ORIGINS", "http://app.agrisence.test", WEAK),
        ("ALLOWED_HOSTS", "", MISSING),
        ("ALLOWED_HOSTS", "*", INVALID),
        ("RATE_LIMITS_DISABLED", "true", INVALID),
        ("SUPABASE_URL", "", MISSING),
        ("SUPABASE_URL", "http://ref.supabase.co", INVALID),
        ("SUPABASE_SERVICE_ROLE_KEY", "", MISSING),
        ("MPESA_ENV", "production", INVALID),
        ("MPESA_BASE_URL", "https://api.safaricom.example", INVALID),
        ("MPESA_SHORTCODE", "600000", INVALID),
        ("MPESA_PASSKEY", "", MISSING),
        ("MPESA_CALLBACK_BASE_URL", "http://api.agrisence.test", INVALID),
        ("MPESA_CALLBACK_TOKEN", "", MISSING),
        ("MPESA_CALLBACK_TOKEN", "short/token", INVALID),
        ("MPESA_CALLBACK_TOKEN", "t" * 28, WEAK),
        ("EMAIL_API_KEY", "", MISSING),
        ("INVITE_CODE_PEPPER", "", MISSING),
        ("INVITE_CODE_PEPPER", "too-short", INVALID),
        ("FRONTEND_BASE_URL", "", MISSING),
        ("FRONTEND_BASE_URL", "http://app.agrisence.test", INVALID),
    ],
)
def test_each_setting_is_classified(setting, value, expected) -> None:
    env = {**valid_production(), setting: value}
    checks = [check for check in run_checks(env) if check.item == setting]
    assert checks and checks[0].status == expected
    assert main(env, env_file=None, out=io.StringIO()) == 1


def test_console_email_is_invalid_in_production_but_fine_in_development() -> None:
    assert status_of({**valid_production(), "EMAIL_PROVIDER": "console"}, "EMAIL_PROVIDER") == INVALID
    development = {**valid_production(), "APP_ENV": "development", "EMAIL_PROVIDER": "console"}
    assert status_of(development, "EMAIL_PROVIDER") == OK


def test_development_defaults_for_hosts_and_a_local_database() -> None:
    env = {
        **valid_production(),
        "APP_ENV": "development",
        "ALLOWED_HOSTS": "",
        "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
    }
    assert status_of(env, "ALLOWED_HOSTS") == OK
    assert status_of(env, "DATABASE_URL") == OK


def test_an_empty_environment_is_reported_missing() -> None:
    out = io.StringIO()
    assert main({}, env_file=None, out=out) == 1
    assert "MISSING" in out.getvalue()
    assert out.getvalue().rstrip().endswith("Environment: development")


def test_no_value_is_ever_printed() -> None:
    env = valid_production()
    variants = [
        env,
        {**env, "JWT_SECRET": "x" * 40},
        {**env, "MPESA_CALLBACK_TOKEN": "bad/token-value-1234567"},
        {**env, "EMAIL_PROVIDER": "smtp", "SMTP_USERNAME": "smtp-user-value", "SMTP_PASSWORD": "smtp-password-value"},
        {**env, "MPESA_STK_TARGET": "test_msisdn", "MPESA_TEST_MSISDN": "254708374149"},
    ]
    for variant in variants:
        out = io.StringIO()
        main(variant, env_file=None, out=out)
        printed = out.getvalue()
        for key, value in variant.items():
            # The environment name is printed on purpose; short enum values may coincide with fixed note words.
            if key in {"APP_ENV", "MPESA_ENV", "MPESA_STK_TARGET", "EMAIL_PROVIDER"} or not value:
                continue
            assert value not in printed, key
        for fragment in (DB_PASSWORD, API_KEY, JWT, PEPPER, CALLBACK, "pooler.supabase.com", "agrisence.test"):
            assert fragment not in printed


def test_env_file_is_read_with_lower_precedence(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('JWT_SECRET="from-file"\nAPP_ENV=staging\n# comment\n', encoding="utf-8")
    merged = check_config.load_environment({"APP_ENV": "production"}, env_file)
    assert merged["JWT_SECRET"] == "from-file"
    assert merged["APP_ENV"] == "production"
