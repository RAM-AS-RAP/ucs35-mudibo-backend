"""Email provider tests: Resend and Brevo over ``httpx.MockTransport``, SMTP with a fake ``aiosmtplib``,
the console mailbox, configuration rules and ``POST /admin/email/test``. Nothing touches the network.
"""

import json
import logging
import ssl
from types import SimpleNamespace

import httpx
import pytest
from invite_fakes import FakeMailer, install, person
from pydantic import ValidationError
from response_helpers import without_request_id

from app.core.config import Settings
from app.integrations import email as email_integration
from app.integrations.email import EmailDeliveryError, EmailMessage, send_email, smtp_error_category
from app.models.enums import UserRole

API_KEY = "re_recognisable_api_key_7c1d9e"
SMTP_PASSWORD = "abcd efgh recognisable smtp app password"
CODE = "K7QX-2MPA"
FROM_ADDRESS = "admin@agrisence.co.ke"


def settings_for(provider, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "jwt_secret": "test-only-jwt-secret-at-least-thirty-two-characters",
        "email_provider": provider,
        "email_from_address": FROM_ADDRESS,
        # Production settings also require explicit allowed hosts (Module 7).
        "allowed_hosts": "api.agrisence.test",
    }
    if provider in ("resend", "brevo"):
        values["email_api_key"] = API_KEY
    if provider == "smtp":
        values.update(smtp_username=FROM_ADDRESS, smtp_password=SMTP_PASSWORD)
    values.update(overrides)
    return Settings(**values)


def message(**overrides) -> EmailMessage:
    values = {
        "to": "grace@example.com",
        "subject": "Your AgriSence administrator invitation",
        "text": f"Your code is {CODE}.",
        "html": f"<p>Your code is {CODE}.</p>",
    }
    values.update(overrides)
    return EmailMessage(**values)


class Recorder:
    def __init__(self, status_code=200, error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.status_code = status_code
        self.error = error

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_code, json={"id": "message-1"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# HTTPS providers -----------------------------------------------------------------------------


async def test_resend_request_format():
    recorder = Recorder()
    settings = settings_for("resend", email_reply_to="support@agrisence.co.ke")
    await send_email(message(), settings=settings, transport=recorder.transport)
    (request,) = recorder.requests
    assert (request.method, str(request.url)) == ("POST", "https://api.resend.com/emails")
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    payload = json.loads(request.content)
    assert payload == {
        "from": f'"Admin via AgriSence" <{FROM_ADDRESS}>',
        "to": ["grace@example.com"],
        "subject": "Your AgriSence administrator invitation",
        "text": f"Your code is {CODE}.",
        "html": f"<p>Your code is {CODE}.</p>",
        "reply_to": "support@agrisence.co.ke",
    }


async def test_resend_omits_optional_fields():
    recorder = Recorder()
    await send_email(message(html=None), settings=settings_for("resend"), transport=recorder.transport)
    payload = json.loads(recorder.requests[0].content)
    assert "html" not in payload and "reply_to" not in payload


async def test_brevo_request_format():
    recorder = Recorder(status_code=201)
    settings = settings_for("brevo", email_reply_to="support@agrisence.co.ke")
    await send_email(message(), settings=settings, transport=recorder.transport)
    (request,) = recorder.requests
    assert (request.method, str(request.url)) == ("POST", "https://api.brevo.com/v3/smtp/email")
    assert request.headers["api-key"] == API_KEY
    assert "authorization" not in request.headers
    payload = json.loads(request.content)
    assert payload == {
        "sender": {"name": "Admin via AgriSence", "email": FROM_ADDRESS},
        "to": [{"email": "grace@example.com"}],
        "subject": "Your AgriSence administrator invitation",
        "textContent": f"Your code is {CODE}.",
        "htmlContent": f"<p>Your code is {CODE}.</p>",
        "replyTo": {"email": "support@agrisence.co.ke"},
    }


@pytest.mark.parametrize("provider", ["resend", "brevo"])
@pytest.mark.parametrize(
    ("recorder", "category"),
    [
        (Recorder(status_code=500), "other"),
        (Recorder(status_code=401), "auth_failed"),
        (Recorder(error=httpx.ConnectTimeout("timed out")), "connection_timeout"),
        (Recorder(error=httpx.ConnectError("refused")), "connection_timeout"),
    ],
)
async def test_https_failures_are_not_retried_and_leak_nothing(provider, recorder, category, caplog):
    caplog.set_level(logging.DEBUG)
    recorder.requests.clear()
    with pytest.raises(EmailDeliveryError) as exc_info:
        await send_email(message(), settings=settings_for(provider), transport=recorder.transport)
    assert len(recorder.requests) == 1
    assert exc_info.value.category == category
    for secret in (API_KEY, CODE, "Your code is"):
        assert secret not in str(exc_info.value)
        assert secret not in caplog.text
    assert "grace@example.com" not in caplog.text
    assert "g***@e***.com" in caplog.text


async def test_a_successful_send_logs_only_provider_masked_recipient_and_outcome(caplog):
    caplog.set_level(logging.DEBUG, logger="app.integrations.email")
    await send_email(message(), settings=settings_for("resend"), transport=Recorder().transport)
    records = [record.getMessage() for record in caplog.records if record.name == "app.integrations.email"]
    assert records == ["email provider=resend to=g***@e***.com result=sent"]


def test_masked_email():
    assert email_integration.mask_email("alice@domain.com") == "a***@d***.com"
    assert email_integration.mask_email("grace@agrisence.co.ke") == "g***@a***.ke"
    assert email_integration.mask_email("broken") == "***"


# Configuration -------------------------------------------------------------------------------


def test_console_is_refused_in_production():
    with pytest.raises(ValidationError):
        settings_for("console", app_env="production", frontend_base_url="https://admin.agrisence.co.ke")


@pytest.mark.parametrize(
    "overrides",
    [
        {"email_provider": "resend", "email_api_key": ""},
        {"email_provider": "brevo", "email_from_address": ""},
        {"email_provider": "smtp", "smtp_password": ""},
        {"email_provider": "smtp", "smtp_username": ""},
        {"email_provider": "resend", "app_env": "production", "frontend_base_url": "http://admin.agrisence.co.ke"},
        {"invite_code_pepper": "too-short"},
        {"invite_max_sends": 6},
        {"smtp_security": "none"},
    ],
)
def test_email_settings_are_validated(overrides):
    overrides = dict(overrides)
    provider = overrides.pop("email_provider", "console")
    with pytest.raises(ValidationError):
        settings_for(provider, **overrides)


def test_email_settings_have_documented_defaults():
    settings = Settings(_env_file=None, jwt_secret="test-only-jwt-secret-at-least-thirty-two-characters")
    assert (settings.email_provider, settings.email_from_name) == ("console", "Admin via AgriSence")
    assert (settings.smtp_host, settings.smtp_port, settings.smtp_security) == ("smtp.gmail.com", 587, "starttls")
    assert settings.smtp_timeout_seconds == 15
    assert (settings.invite_ttl_days, settings.invite_code_ttl_minutes, settings.invite_max_attempts) == (7, 15, 10)
    assert (settings.invite_max_sends, settings.invite_resend_cooldown_seconds, settings.invite_max_pending) == (
        5,
        60,
        10,
    )
    assert (settings.reset_code_ttl_minutes, settings.reset_max_attempts) == (15, 5)
    assert settings.reset_max_requests_per_hour == 3
    assert settings.codes_configured is False
    assert "validate_certs" not in Settings.model_fields and "smtp_verify" not in " ".join(Settings.model_fields)


# Console -------------------------------------------------------------------------------------


async def test_console_writes_the_code_only_to_the_dev_mailbox(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(email_integration, "DEV_MAILBOX_DIR", tmp_path / ".dev_mailbox")
    await send_email(message(), settings=settings_for("console"))
    files = list((tmp_path / ".dev_mailbox").glob("*.txt"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert CODE in content and "To: grace@example.com" in content
    assert CODE not in caplog.text and "grace@example.com" not in caplog.text
    assert [record.getMessage() for record in caplog.records if record.name == "app.integrations.email"] == [
        "dev email written"
    ]


# SMTP ----------------------------------------------------------------------------------------


class FakeSmtp:
    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self.error = error

    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return ({}, "OK")


@pytest.fixture
def smtp(monkeypatch):
    fake = FakeSmtp()
    monkeypatch.setattr(email_integration, "aiosmtplib", SimpleNamespace(send=fake.send))
    return fake


async def test_smtp_builds_the_message_with_the_display_name(smtp):
    settings = settings_for("smtp", email_reply_to="support@agrisence.co.ke")
    await send_email(message(), settings=settings)
    ((args, kwargs),) = smtp.calls
    mime = args[0]
    assert "Admin via AgriSence" in mime["From"] and FROM_ADDRESS in mime["From"]
    assert mime["To"] == "grace@example.com"
    assert mime["Subject"] == "Your AgriSence administrator invitation"
    assert mime["Reply-To"] == "support@agrisence.co.ke"
    assert mime["Date"] and mime["Message-ID"].endswith("@agrisence.co.ke>")
    assert mime.get_body(("plain",)).get_content().strip() == f"Your code is {CODE}."
    assert CODE in mime.get_body(("html",)).get_content()


@pytest.mark.parametrize(
    ("port", "security", "start_tls", "use_tls"),
    [(587, "starttls", True, False), (465, "ssl", False, True)],
)
async def test_smtp_tls_modes_and_timeout(smtp, port, security, start_tls, use_tls):
    settings = settings_for("smtp", smtp_port=port, smtp_security=security, smtp_timeout_seconds=12)
    await send_email(message(), settings=settings)
    ((_, kwargs),) = smtp.calls
    assert (kwargs["start_tls"], kwargs["use_tls"]) == (start_tls, use_tls)
    assert kwargs["timeout"] == 12
    assert (kwargs["hostname"], kwargs["port"]) == ("smtp.gmail.com", port)
    assert (kwargs["username"], kwargs["password"]) == (FROM_ADDRESS, SMTP_PASSWORD)
    # Certificate verification stays at aiosmtplib's secure defaults.
    assert "validate_certs" not in kwargs and "tls_context" not in kwargs and "cert_bundle" not in kwargs
    assert set(kwargs) == {"hostname", "port", "username", "password", "start_tls", "use_tls", "timeout"}


@pytest.mark.parametrize(
    ("message_overrides", "settings_overrides"),
    [
        ({"subject": "Hello\r\nBcc: victim@example.com"}, {}),
        ({"to": "grace@example.com\nBcc: victim@example.com"}, {}),
        ({}, {"email_from_name": "Admin\r\nBcc: victim@example.com"}),
        ({}, {"email_reply_to": "a@example.com\nX-Injected: 1"}),
    ],
)
async def test_line_breaks_in_headers_are_rejected_before_any_send(smtp, message_overrides, settings_overrides):
    # model_copy skips validation, as a value that slipped past the settings validators would.
    settings = settings_for("smtp").model_copy(update=settings_overrides)
    with pytest.raises(EmailDeliveryError) as exc_info:
        await send_email(message(**message_overrides), settings=settings)
    assert exc_info.value.category == "invalid_header"
    assert smtp.calls == []


def test_settings_reject_line_breaks_in_header_values():
    with pytest.raises(ValidationError):
        settings_for("smtp", email_from_name="Admin\r\nBcc: victim@example.com")


def smtp_error(name: str, *bases: type) -> Exception:
    """A stand-in with aiosmtplib's class name, so the mapping does not depend on the package."""
    return type(name, bases or (Exception,), {})(f"server said: {SMTP_PASSWORD}")


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (smtp_error("SMTPAuthenticationError"), "auth_failed"),
        (smtp_error("SMTPRecipientsRefused"), "recipient_refused"),
        (smtp_error("SMTPRecipientRefused"), "recipient_refused"),
        (smtp_error("SMTPConnectError", ConnectionError), "connection_timeout"),
        (smtp_error("SMTPConnectTimeoutError", TimeoutError), "connection_timeout"),
        (smtp_error("SMTPTimeoutError", TimeoutError), "connection_timeout"),
        (smtp_error("SMTPServerDisconnected", ConnectionError), "connection_timeout"),
        (TimeoutError("timed out"), "connection_timeout"),
        (ConnectionRefusedError("refused"), "connection_timeout"),
        (OSError("Network is unreachable"), "connection_timeout"),
        (ssl.SSLCertVerificationError("certificate verify failed"), "tls_failed"),
        (ssl.SSLError("wrong version number"), "tls_failed"),
        (smtp_error("SMTPException"), "other"),
        (ValueError("unexpected"), "other"),
    ],
)
async def test_smtp_errors_map_to_categories_without_retry(monkeypatch, error, category, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeSmtp(error=error)
    monkeypatch.setattr(email_integration, "aiosmtplib", SimpleNamespace(send=fake.send))
    with pytest.raises(EmailDeliveryError) as exc_info:
        await send_email(message(), settings=settings_for("smtp"))
    assert exc_info.value.category == category
    assert len(fake.calls) == 1
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__
    for secret in (SMTP_PASSWORD, CODE, "server said"):
        assert secret not in str(exc_info.value)
        assert secret not in caplog.text


def test_a_starttls_failure_wrapped_by_aiosmtplib_is_tls_failed():
    wrapper = smtp_error("SMTPConnectError", ConnectionError)
    wrapper.__cause__ = ssl.SSLError("handshake failure")
    assert smtp_error_category(wrapper) == "tls_failed"


def test_real_aiosmtplib_exceptions_map_when_installed():
    aiosmtplib = pytest.importorskip("aiosmtplib")
    assert smtp_error_category(aiosmtplib.SMTPAuthenticationError(535, "bad")) == "auth_failed"
    assert smtp_error_category(aiosmtplib.SMTPConnectTimeoutError("slow")) == "connection_timeout"
    assert smtp_error_category(aiosmtplib.SMTPRecipientsRefused([])) == "recipient_refused"


# Startup warnings ----------------------------------------------------------------------------


def warnings_for(caplog, settings) -> str:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.integrations.email"):
        email_integration.log_startup_warnings(settings)
    return caplog.text


def test_smtp_in_production_logs_the_blocked_ports_warning(caplog):
    settings = settings_for("smtp", app_env="production", frontend_base_url="https://admin.agrisence.co.ke")
    text = warnings_for(caplog, settings)
    assert "block SMTP ports 25, 465 and 587" in text
    assert SMTP_PASSWORD not in text


def test_port_and_security_mismatch_is_warned(caplog):
    assert "does not match" in warnings_for(caplog, settings_for("smtp", smtp_port=465, smtp_security="starttls"))
    assert "does not match" in warnings_for(caplog, settings_for("smtp", smtp_port=587, smtp_security="ssl"))
    assert "does not match" not in warnings_for(caplog, settings_for("smtp", smtp_port=465, smtp_security="ssl"))


def test_gmail_sender_rewrite_is_warned(caplog):
    settings = settings_for("smtp", smtp_username="agrisence.admin@gmail.com")
    assert "Gmail replaces the sender address" in warnings_for(caplog, settings)
    assert warnings_for(caplog, settings_for("smtp")) == ""


def test_other_providers_log_no_smtp_warning(caplog):
    settings = settings_for("resend", app_env="production", frontend_base_url="https://admin.agrisence.co.ke")
    assert warnings_for(caplog, settings) == ""


# POST /admin/email/test ----------------------------------------------------------------------


async def test_email_check_sends_to_the_super_admins_own_address(api_client, application, monkeypatch):
    mailer = FakeMailer()
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)
    install(application, person(super_admin=True))
    response = await api_client.post("/api/v1/admin/email/test")
    assert (response.status_code, response.json()) == (200, {"email_sent": True})
    (sent,) = mailer.sent
    assert sent.to == "super@example.com"
    assert "code" not in sent.subject.lower()
    assert "no code" in sent.text


async def test_email_check_failure_returns_only_the_category(api_client, application, monkeypatch):
    error = EmailDeliveryError("auth_failed")
    mailer = FakeMailer(error=error)
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)
    install(application, person(super_admin=True))
    response = await api_client.post("/api/v1/admin/email/test")
    assert response.status_code == 502
    assert without_request_id(response.json()) == {
        "error": {"code": "email_failed", "message": "The email could not be sent.", "category": "auth_failed"}
    }


async def test_email_check_through_smtp_never_returns_the_password(api_client, application, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    settings = settings_for("smtp")
    monkeypatch.setattr(email_integration, "get_settings", lambda: settings)
    fake = FakeSmtp(error=smtp_error("SMTPAuthenticationError"))
    monkeypatch.setattr(email_integration, "aiosmtplib", SimpleNamespace(send=fake.send))
    install(application, person(super_admin=True))
    response = await api_client.post("/api/v1/admin/email/test")
    assert response.status_code == 502
    assert response.json()["error"]["category"] == "auth_failed"
    assert SMTP_PASSWORD not in response.text and SMTP_PASSWORD not in caplog.text


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER, UserRole.ADMIN])
async def test_email_check_is_forbidden_to_everyone_but_the_super_admin(api_client, application, role):
    install(application, person(role))
    response = await api_client.post("/api/v1/admin/email/test")
    assert response.status_code == 403


async def test_email_check_rejects_a_body(api_client, application):
    install(application, person(super_admin=True))
    response = await api_client.post("/api/v1/admin/email/test", json={"to": "someone@example.com"})
    assert response.status_code == 422
