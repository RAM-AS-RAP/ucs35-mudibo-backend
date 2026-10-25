"""Source checks and an end-to-end secrets check for the invite and reset addendum."""

import importlib
import json
import logging
import pkgutil
import re
from pathlib import Path

import httpx
from invite_fakes import FakeMailer, RecordingDB, configure_codes, inserted_invite, install, person
from payment_fakes import FakeResult
from pydantic import BaseModel

import app.schemas
from app.core.config import Settings, get_settings
from app.integrations import email as email_integration

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
DOCS_DIR = ROOT / "docs"
NEW_FILES = [
    "core/codes.py",
    "core/password_policy.py",
    "integrations/email.py",
    "integrations/email_templates.py",
    "routers/admin_invites.py",
    "schemas/invite.py",
    "services/invite_service.py",
    "services/password_reset_service.py",
    "services/registration_service.py",
    "services/auth_service.py",
    "services/email_delivery.py",
    "core/auth_result.py",
]
# Request bodies that carry a code typed by the user; no other schema may have a code field.
CODE_REQUEST_SCHEMAS = {
    "AcceptInviteRequest",
    "PasswordResetConfirmRequest",
    "RegisterVerifyRequest",
    "LoginVerifyRequest",
}
IMPORT_LINE = re.compile(r"^\s*(?:import|from)\s+(\S+)", re.MULTILINE)


def imported_modules(path: Path) -> list[str]:
    return IMPORT_LINE.findall(path.read_text(encoding="utf-8"))


def test_no_temporary_password_anywhere_in_app_or_docs():
    for directory, pattern in ((APP_DIR, "*.py"), (DOCS_DIR, "*.md")):
        for path in directory.rglob(pattern):
            assert "temporary_password" not in path.read_text(encoding="utf-8"), path
    assert not hasattr(get_settings(), "admin_temp_password_bytes")


def test_no_response_schema_has_a_password_or_code_hash(application):
    for module_info in pkgutil.iter_modules(app.schemas.__path__):
        module = importlib.import_module(f"app.schemas.{module_info.name}")
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseModel):
                for field in ("password_hash", "code_hash", "code"):
                    if field == "code" and value.__name__ in CODE_REQUEST_SCHEMAS:
                        continue
                    assert field not in value.model_fields, f"{value.__name__}.{field}"
    schemas = json.dumps(application.openapi()["components"]["schemas"])
    assert "password_hash" not in schemas and "code_hash" not in schemas


def test_no_float_in_the_new_services_and_schemas():
    for name in ("services/invite_service.py", "services/password_reset_service.py", "schemas/invite.py"):
        assert re.search(r"\bfloat\b", (APP_DIR / name).read_text(encoding="utf-8")) is None, name


def test_aiosmtplib_only_in_the_email_integration_and_smtplib_nowhere():
    for path in APP_DIR.rglob("*.py"):
        modules = [module.split(".")[0] for module in imported_modules(path)]
        assert "smtplib" not in modules, path
        if path != APP_DIR / "integrations" / "email.py":
            assert "aiosmtplib" not in modules, path
    assert "aiosmtplib" in [module.split(".")[0] for module in imported_modules(APP_DIR / "integrations/email.py")]
    for path in (ROOT / "scripts").rglob("*.py"):
        assert "smtplib" not in [module.split(".")[0] for module in imported_modules(path)], path


def test_no_certificate_verification_switch_exists():
    source = (APP_DIR / "integrations" / "email.py").read_text(encoding="utf-8")
    assert "validate_certs" not in source and "CERT_NONE" not in source and "check_hostname" not in source
    assert not any("verify" in field or "cert" in field for field in Settings.model_fields)


def test_no_code_is_logged_or_put_in_a_url():
    for name in NEW_FILES:
        source = (APP_DIR / name).read_text(encoding="utf-8")
        for line in source.splitlines():
            if "logger." in line:
                for token in ("code_hash", "code)", ".password", "new_password", "api_key", "smtp_password", ".text"):
                    assert token not in line, line
        assert "?code=" not in source and "/accept-invite/" not in source and "/reset-password/" not in source


def test_aiosmtplib_is_a_requirement():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").split()
    assert "aiosmtplib" in requirements


async def test_codes_and_api_keys_never_leak(api_client, application, monkeypatch, caplog):
    """A recognisable API key and the generated code appear in no log, response, error or stored value."""
    caplog.set_level(logging.DEBUG)
    configure_codes(monkeypatch, get_settings())
    api_key = "re_recognisable_key_for_leak_check_51f0"
    settings = Settings(
        _env_file=None,
        jwt_secret="test-only-jwt-secret-at-least-thirty-two-characters",
        email_provider="resend",
        email_api_key=api_key,
        email_from_address="admin@agrisence.co.ke",
    )
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"message": f"rejected key {api_key}"})

    real_send = email_integration.send_email
    errors: list[Exception] = []
    recorder = FakeMailer()

    async def send(message, **kwargs):
        recorder.sent.append(message)
        try:
            await real_send(message, settings=settings, transport=httpx.MockTransport(provider))
        except email_integration.EmailDeliveryError as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(email_integration, "send_email", send)
    db = install(
        application,
        person(super_admin=True),
        RecordingDB(scalars=[False, 0], results=[FakeResult(), FakeResult(), inserted_invite]),
    )

    response = await api_client.post(
        "/api/v1/admin/invites",
        json={"name": "Grace Admin", "email": "grace@example.com"},
    )

    assert response.status_code == 201 and response.json()["email_sent"] is False
    code = recorder.code()
    assert len(requests) == 1 and code in requests[0].content.decode().replace("-", "")
    stored = json.dumps([{key: str(value) for key, value in params.items()} for params in db.params])
    texts = {"logs": caplog.text, "response": response.text, "error": str(errors[0]), "stored": stored}
    for where, text in texts.items():
        assert api_key not in text, where
        assert code not in text.replace("-", ""), where
