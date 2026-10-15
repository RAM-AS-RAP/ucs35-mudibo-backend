"""Check the configuration of the current environment without printing any value.

Usage (from the repository root, in the environment to check):

    python scripts/check_config.py          # or: python -m scripts.check_config

It reads the process environment and, for keys not set there, a ``.env`` file in the current directory
(the same precedence as the application). It never connects to the database or the network and never
prints a setting's value, only a status per check:

    OK        the setting is present and usable
    MISSING   a required setting is empty
    WEAK      present but below the recommended strength, or risky for this environment
    INVALID   present but the application would refuse it, or it breaks a production rule

The exit code is 0 when every check is OK and 1 otherwise. The last line names the environment.
"""

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

OK, MISSING, WEAK, INVALID = "OK", "MISSING", "WEAK", "INVALID"
FAILING = {MISSING, WEAK, INVALID}

SANDBOX_BASE_URL = "https://sandbox.safaricom.co.ke"
SANDBOX_SHORTCODE = "174379"
TRANSACTION_POOLER_PORT = 6543
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_URL_SAFE = re.compile(r"[A-Za-z0-9_-]+")
_PHONE = re.compile(r"254[17][0-9]{8}")
_HOST = re.compile(r"(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?")
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Check:
    area: str
    item: str
    status: str
    note: str


def load_environment(environ: Mapping[str, str], env_file: Path | None = Path(".env")) -> dict[str, str]:
    """Merge ``.env`` (lower precedence) with the process environment; keys are upper-cased."""
    values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.upper()] = value
    values.update({key.upper(): value for key, value in environ.items()})
    return values


def _get(env: Mapping[str, str], key: str) -> str:
    return env.get(key, "").strip()


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _origin_ok(origin: str) -> bool:
    parts = urlsplit(origin)
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and not (parts.path or parts.query or parts.fragment)
    )


def environment_name(env: Mapping[str, str]) -> str:
    return _get(env, "APP_ENV") or "development"


def is_production(env: Mapping[str, str]) -> bool:
    return environment_name(env).lower() == "production"


def check_database(env: Mapping[str, str]) -> list[Check]:
    url = _get(env, "DATABASE_URL")
    if not url:
        return [Check("database", "DATABASE_URL", MISSING, "required for every database route")]
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres", "postgresql+asyncpg"):
        return [Check("database", "DATABASE_URL", INVALID, "scheme must be postgresql://")]
    try:
        port = parts.port
    except ValueError:
        return [Check("database", "DATABASE_URL", INVALID, "port is not a number")]
    if not parts.hostname or not parts.username or parts.password is None:
        return [Check("database", "DATABASE_URL", INVALID, "host, user and password are required")]
    if parts.hostname in LOCAL_HOSTS:
        if is_production(env):
            return [Check("database", "DATABASE_URL", INVALID, "a local database in production")]
        return [Check("database", "DATABASE_URL", OK, "local database (development)")]
    if port != TRANSACTION_POOLER_PORT:
        return [Check("database", "DATABASE_URL", WEAK, "use the Supabase transaction pooler (port 6543)")]
    return [Check("database", "DATABASE_URL", OK, "postgresql via the transaction pooler")]


def check_jwt(env: Mapping[str, str]) -> list[Check]:
    secret = _get(env, "JWT_SECRET")
    if not secret:
        return [Check("auth", "JWT_SECRET", MISSING, "the application refuses to start")]
    if len(secret) < 32:
        return [Check("auth", "JWT_SECRET", INVALID, "fewer than 32 characters; the application refuses it")]
    if len(secret) < 48:
        return [Check("auth", "JWT_SECRET", WEAK, "use at least 48 random characters")]
    return [Check("auth", "JWT_SECRET", OK, f"{len(secret)} characters")]


def check_cors_and_hosts(env: Mapping[str, str]) -> list[Check]:
    production = is_production(env)
    checks = []
    origins = _csv(_get(env, "CORS_ORIGINS"))
    if not origins:
        status = MISSING if production else OK
        checks.append(Check("http", "CORS_ORIGINS", status, "no browser origin is allowed"))
    elif "*" in origins:
        status = INVALID if production else WEAK
        checks.append(Check("http", "CORS_ORIGINS", status, "wildcard origin"))
    elif not all(_origin_ok(origin) for origin in origins):
        checks.append(Check("http", "CORS_ORIGINS", INVALID, "each entry must be scheme://host[:port] with no path"))
    elif production and any(origin.startswith("http://") for origin in origins):
        checks.append(Check("http", "CORS_ORIGINS", WEAK, "plain http origin in production"))
    else:
        checks.append(Check("http", "CORS_ORIGINS", OK, f"{len(origins)} origin(s)"))

    hosts = _csv(_get(env, "ALLOWED_HOSTS"))
    if not hosts:
        status = MISSING if production else OK
        checks.append(Check("http", "ALLOWED_HOSTS", status, "defaults to * outside production"))
    elif any("*" in host for host in hosts):
        status = INVALID if production else OK
        checks.append(Check("http", "ALLOWED_HOSTS", status, "wildcard host"))
    elif not all(_HOST.fullmatch(host) for host in hosts):
        checks.append(Check("http", "ALLOWED_HOSTS", INVALID, "host names only, no scheme or path"))
    else:
        checks.append(Check("http", "ALLOWED_HOSTS", OK, f"{len(hosts)} host(s)"))

    disabled = _get(env, "RATE_LIMITS_DISABLED").lower() in _TRUE
    if disabled and production:
        checks.append(Check("http", "RATE_LIMITS_DISABLED", INVALID, "refused in production"))
    elif disabled:
        checks.append(Check("http", "RATE_LIMITS_DISABLED", WEAK, "rate limits are off (development aid only)"))
    else:
        checks.append(Check("http", "RATE_LIMITS_DISABLED", OK, "rate limits are on"))
    return checks


def check_supabase(env: Mapping[str, str]) -> list[Check]:
    checks = []
    url = _get(env, "SUPABASE_URL")
    if not url:
        checks.append(Check("storage", "SUPABASE_URL", MISSING, "listing images cannot be stored"))
    elif not url.startswith("https://"):
        checks.append(Check("storage", "SUPABASE_URL", INVALID, "must start with https://"))
    else:
        checks.append(Check("storage", "SUPABASE_URL", OK, "present"))
    key = _get(env, "SUPABASE_SERVICE_ROLE_KEY")
    checks.append(Check("storage", "SUPABASE_SERVICE_ROLE_KEY", OK if key else MISSING, "server side only"))
    return checks


def check_mpesa(env: Mapping[str, str]) -> list[Check]:
    checks = []
    mpesa_env = _get(env, "MPESA_ENV") or "sandbox"
    checks.append(
        Check("payments", "MPESA_ENV", OK if mpesa_env == "sandbox" else INVALID, "only the sandbox is supported")
    )
    base_url = _get(env, "MPESA_BASE_URL") or SANDBOX_BASE_URL
    checks.append(
        Check("payments", "MPESA_BASE_URL", OK if base_url == SANDBOX_BASE_URL else INVALID, "sandbox URL only")
    )
    shortcode = _get(env, "MPESA_SHORTCODE") or SANDBOX_SHORTCODE
    checks.append(
        Check("payments", "MPESA_SHORTCODE", OK if shortcode == SANDBOX_SHORTCODE else INVALID, "sandbox shortcode")
    )
    for key in ("MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_PASSKEY"):
        checks.append(Check("payments", key, OK if _get(env, key) else MISSING, "required for STK push"))

    callback_base = _get(env, "MPESA_CALLBACK_BASE_URL")
    if not callback_base:
        checks.append(Check("payments", "MPESA_CALLBACK_BASE_URL", MISSING, "Daraja cannot reach the callback"))
    elif not callback_base.startswith("https://"):
        checks.append(Check("payments", "MPESA_CALLBACK_BASE_URL", INVALID, "must be https"))
    else:
        checks.append(Check("payments", "MPESA_CALLBACK_BASE_URL", OK, "https"))

    token = _get(env, "MPESA_CALLBACK_TOKEN")
    if not token:
        checks.append(Check("payments", "MPESA_CALLBACK_TOKEN", MISSING, "payment routes answer 503"))
    elif len(token) < 24 or not _URL_SAFE.fullmatch(token):
        checks.append(Check("payments", "MPESA_CALLBACK_TOKEN", INVALID, "at least 24 URL-safe characters"))
    elif len(token) < 32:
        checks.append(Check("payments", "MPESA_CALLBACK_TOKEN", WEAK, "use at least 32 characters"))
    else:
        checks.append(Check("payments", "MPESA_CALLBACK_TOKEN", OK, f"{len(token)} characters"))

    target = _get(env, "MPESA_STK_TARGET") or "registered"
    if target not in ("registered", "test_msisdn"):
        checks.append(Check("payments", "MPESA_STK_TARGET", INVALID, "registered or test_msisdn"))
    elif target == "test_msisdn" and not _PHONE.fullmatch(_get(env, "MPESA_TEST_MSISDN") or "254708374149"):
        checks.append(Check("payments", "MPESA_TEST_MSISDN", INVALID, "must match 254[17]XXXXXXXX"))
    else:
        checks.append(Check("payments", "MPESA_STK_TARGET", OK, "a supported prompt target"))
    return checks


def check_email(env: Mapping[str, str]) -> list[Check]:
    provider = (_get(env, "EMAIL_PROVIDER") or "console").lower()
    production = is_production(env)
    if provider == "console":
        status = INVALID if production else OK
        return [Check("email", "EMAIL_PROVIDER", status, "console writes to .dev_mailbox/ (development only)")]
    if provider in ("resend", "brevo"):
        missing = [key for key in ("EMAIL_API_KEY", "EMAIL_FROM_ADDRESS") if not _get(env, key)]
        if missing:
            return [Check("email", key, MISSING, "required for the HTTPS email providers") for key in missing]
        return [Check("email", "EMAIL_PROVIDER", OK, "HTTPS email API configured")]
    if provider == "smtp":
        required = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM_ADDRESS")
        # SMTP_HOST has a default (smtp.gmail.com) in the application settings.
        missing = [key for key in required if key != "SMTP_HOST" and not _get(env, key)]
        if missing:
            return [Check("email", key, MISSING, "required for smtp") for key in missing]
        if production:
            return [Check("email", "EMAIL_PROVIDER", WEAK, "many hosts block SMTP ports; prefer an HTTPS provider")]
        return [Check("email", "EMAIL_PROVIDER", OK, "smtp")]
    return [Check("email", "EMAIL_PROVIDER", INVALID, "console, smtp, resend or brevo")]


def check_codes_and_frontend(env: Mapping[str, str]) -> list[Check]:
    checks = []
    pepper = _get(env, "INVITE_CODE_PEPPER")
    if not pepper:
        checks.append(Check("codes", "INVITE_CODE_PEPPER", MISSING, "invite and reset routes answer 503"))
    elif len(pepper) < 32:
        checks.append(Check("codes", "INVITE_CODE_PEPPER", INVALID, "fewer than 32 characters"))
    else:
        checks.append(Check("codes", "INVITE_CODE_PEPPER", OK, f"{len(pepper)} characters"))

    frontend = _get(env, "FRONTEND_BASE_URL")
    if not frontend:
        checks.append(Check("codes", "FRONTEND_BASE_URL", MISSING, "emails cannot link to the frontend"))
    elif urlsplit(frontend).scheme not in ("http", "https") or not urlsplit(frontend).hostname:
        checks.append(Check("codes", "FRONTEND_BASE_URL", INVALID, "must be an http(s) URL"))
    elif is_production(env) and not frontend.startswith("https://"):
        checks.append(Check("codes", "FRONTEND_BASE_URL", INVALID, "must be https in production"))
    else:
        checks.append(Check("codes", "FRONTEND_BASE_URL", OK, "present"))
    return checks


def run_checks(env: Mapping[str, str]) -> list[Check]:
    """Run every check against ``env`` (a mapping of setting names to raw values)."""
    return [
        *check_database(env),
        *check_jwt(env),
        *check_cors_and_hosts(env),
        *check_supabase(env),
        *check_mpesa(env),
        *check_email(env),
        *check_codes_and_frontend(env),
    ]


def render(checks: list[Check], env: Mapping[str, str]) -> str:
    """The report table; it contains names, statuses and fixed notes only, never a value."""
    headers = ("AREA", "SETTING", "STATUS", "NOTE")
    rows = [headers, *((check.area, check.item, check.status, check.note) for check in checks)]
    widths = [max(len(row[index]) for row in rows) for index in range(3)]
    lines = [
        "  ".join(cell.ljust(widths[index]) if index < 3 else cell for index, cell in enumerate(row)) for row in rows
    ]
    failing = sum(check.status in FAILING for check in checks)
    lines.append("")
    lines.append(f"{len(checks) - failing} OK, {failing} need attention.")
    # The environment name is the only value printed; it is not a secret.
    lines.append(f"Environment: {environment_name(env)}")
    return "\n".join(lines)


def main(
    environ: Mapping[str, str] | None = None, env_file: Path | None = Path(".env"), out: TextIO = sys.stdout
) -> int:
    env = load_environment(os.environ if environ is None else environ, env_file)
    checks = run_checks(env)
    print(render(checks, env), file=out)
    return 1 if any(check.status in FAILING for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
