"""Transactional email: registration, sign in, admin invitation and password reset codes.

Email is a hard dependency of sign in: farmers, buyers and administrators confirm their address with a
code when they register (or accept an invite) and, for the roles in ``LOGIN_OTP_ROLES``, receive a code
after every correct password.

``send_email`` dispatches on ``EMAIL_PROVIDER``:

- ``console`` (development only, refused when ``APP_ENV=production``) writes the whole message to
  ``./.dev_mailbox/`` and logs only "dev email written".
- ``smtp`` sends through an SMTP server (for example Gmail with an app password) using ``aiosmtplib``
  with STARTTLS (port 587) or implicit TLS (port 465) and certificate verification always on.
- ``resend`` and ``brevo`` call the providers' HTTPS APIs.

SMTP ports can be blocked by hosting platforms: Render free web services, for example, block
outbound ports 25, 465 and 587. On such a host an HTTPS provider (Resend or Brevo) must be used, which
is why the HTTPS providers exist alongside SMTP.

The Resend and Brevo request formats below follow each provider's documentation at the time of
writing and must be re-checked against that documentation (https://resend.com/docs/api-reference and
https://developers.brevo.com/reference/sendtransacemail) before relying on them.

Sends are never retried automatically. Every failure raises ``EmailDeliveryError`` with a fixed, safe
message and a category; it never contains the API key, the SMTP password, the code, the body or raw
library text. Logs carry only the provider name, a masked recipient and the outcome.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings, get_settings

try:  # aiosmtplib is listed in requirements.txt; the API still starts without it (smtp sends then fail).
    import aiosmtplib
except ImportError:  # pragma: no cover - depends on the installed packages
    aiosmtplib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEV_MAILBOX_DIR = Path(".dev_mailbox")

CATEGORY_MESSAGES = {
    "connection_timeout": "The email server could not be reached in time.",
    "auth_failed": "The email server rejected the sending credentials.",
    "tls_failed": "A secure connection to the email server could not be established.",
    "recipient_refused": "The email server refused the recipient address.",
    "invalid_header": "The email contains an invalid header value.",
    "not_configured": "Email sending is not configured on this server.",
    "other": "The email could not be sent.",
}


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None


class EmailDeliveryError(Exception):
    """A failed send. ``category`` and ``message`` are fixed strings that are safe to log and return."""

    def __init__(self, category: str = "other") -> None:
        self.category = category if category in CATEGORY_MESSAGES else "other"
        self.message = CATEGORY_MESSAGES[self.category]
        super().__init__(self.message)


def mask_email(address: str) -> str:
    """Mask an address as ``a***@d***.com`` for logs and responses."""
    local, _, domain = address.strip().partition("@")
    if not local or not domain:
        return "***"
    tld = domain.rsplit(".", 1)[1] if "." in domain else ""
    masked_domain = f"{domain[0]}***" + (f".{tld}" if tld else "")
    return f"{local[0]}***@{masked_domain}"


def _reject_header_injection(message: EmailMessage, settings: Settings) -> None:
    values = (
        message.to,
        message.subject,
        settings.email_from_name,
        settings.email_from_address,
        settings.email_reply_to,
    )
    if any("\r" in value or "\n" in value for value in values):
        raise EmailDeliveryError("invalid_header")
    if not message.to.strip():
        raise EmailDeliveryError("invalid_header")


async def send_email(
    message: EmailMessage, *, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """Send one message with the configured provider, exactly once (no automatic retry)."""
    settings = settings or get_settings()
    provider = settings.email_provider
    _reject_header_injection(message, settings)
    if provider == "console":
        await asyncio.to_thread(_write_dev_mailbox, message)
        logger.info("dev email written")
        return
    try:
        if provider == "smtp":
            await _send_smtp(message, settings)
        elif provider == "resend":
            await _send_resend(message, settings, transport)
        elif provider == "brevo":
            await _send_brevo(message, settings, transport)
        else:
            raise EmailDeliveryError("not_configured")
    except EmailDeliveryError as exc:
        logger.warning(
            "email provider=%s to=%s result=failed category=%s", provider, mask_email(message.to), exc.category
        )
        raise
    logger.info("email provider=%s to=%s result=sent", provider, mask_email(message.to))


# Console --------------------------------------------------------------------------------------


def _write_dev_mailbox(message: EmailMessage) -> Path:
    DEV_MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = DEV_MAILBOX_DIR / f"{stamp}-{secrets.token_hex(4)}.txt"
    parts = [f"To: {message.to}", f"Subject: {message.subject}", "", message.text]
    if message.html:
        parts += ["", "--- HTML ---", message.html]
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return path


# HTTPS providers ------------------------------------------------------------------------------


async def _post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], transport: httpx.AsyncBaseTransport | None
) -> None:
    if not url.startswith("https://"):
        raise EmailDeliveryError("not_configured")
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=transport, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise EmailDeliveryError("connection_timeout") from None
    except httpx.TransportError:
        raise EmailDeliveryError("connection_timeout") from None
    if response.status_code in (401, 403):
        raise EmailDeliveryError("auth_failed")
    if not 200 <= response.status_code < 300:
        raise EmailDeliveryError("other")


async def _send_resend(message: EmailMessage, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
    payload: dict[str, Any] = {
        "from": f'"{settings.email_from_name}" <{settings.email_from_address}>',
        "to": [message.to],
        "subject": message.subject,
        "text": message.text,
    }
    if message.html:
        payload["html"] = message.html
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    headers = {"Authorization": f"Bearer {settings.email_api_key}"}
    await _post_json(RESEND_URL, headers, payload, transport)


async def _send_brevo(message: EmailMessage, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
    payload: dict[str, Any] = {
        "sender": {"name": settings.email_from_name, "email": settings.email_from_address},
        "to": [{"email": message.to}],
        "subject": message.subject,
        "textContent": message.text,
    }
    if message.html:
        payload["htmlContent"] = message.html
    if settings.email_reply_to:
        payload["replyTo"] = {"email": settings.email_reply_to}
    headers = {"api-key": settings.email_api_key, "accept": "application/json"}
    await _post_json(BREVO_URL, headers, payload, transport)


# SMTP -----------------------------------------------------------------------------------------


def build_mime_message(message: EmailMessage, settings: Settings) -> MimeMessage:
    """Build the MIME message; header values were already checked for CR and LF."""
    mime = MimeMessage()
    mime["From"] = formataddr((settings.email_from_name, settings.email_from_address))
    mime["To"] = message.to
    mime["Subject"] = message.subject
    if settings.email_reply_to:
        mime["Reply-To"] = settings.email_reply_to
    mime["Date"] = formatdate(usegmt=True)
    domain = settings.email_from_address.rpartition("@")[2] or None
    mime["Message-ID"] = make_msgid(domain=domain)
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    return mime


_AUTH_ERRORS = {"SMTPAuthenticationError"}
_RECIPIENT_ERRORS = {"SMTPRecipientRefused", "SMTPRecipientsRefused"}
_TLS_ERRORS = {"SSLError", "SSLCertVerificationError", "CertificateError"}
_CONNECTION_ERRORS = {
    "SMTPConnectError",
    "SMTPConnectTimeoutError",
    "SMTPConnectResponseError",
    "SMTPTimeoutError",
    "SMTPReadTimeoutError",
    "SMTPServerDisconnected",
}


def smtp_error_category(exc: BaseException) -> str:
    """Map an aiosmtplib (or socket/TLS) exception to a category, by class name along its MRO.

    Matching by name keeps this module importable without aiosmtplib and lets tests use stand-ins.
    The exception text is never returned or logged.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _AUTH_ERRORS:
        return "auth_failed"
    if names & _RECIPIENT_ERRORS:
        return "recipient_refused"
    cause = exc.__cause__
    if isinstance(exc, ssl.SSLError) or names & _TLS_ERRORS or isinstance(cause, ssl.SSLError):
        return "tls_failed"
    if "SMTPException" in names and "tls" in str(exc).lower():
        return "tls_failed"
    if names & _CONNECTION_ERRORS or isinstance(exc, TimeoutError | ConnectionError | OSError):
        return "connection_timeout"
    return "other"


async def _send_smtp(message: EmailMessage, settings: Settings) -> None:
    if aiosmtplib is None:
        raise EmailDeliveryError("not_configured")
    mime = build_mime_message(message, settings)
    try:
        # Default TLS settings: the server certificate and host name are always verified.
        await aiosmtplib.send(
            mime,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            start_tls=settings.smtp_security == "starttls",
            use_tls=settings.smtp_security == "ssl",
            timeout=settings.smtp_timeout_seconds,
        )
    except Exception as exc:  # every failure becomes a safe, categorised error
        raise EmailDeliveryError(smtp_error_category(exc)) from None


# Startup --------------------------------------------------------------------------------------


def log_startup_warnings(settings: Settings) -> None:
    """Log configuration warnings once at startup; never fails and never logs a secret."""
    if settings.email_provider != "smtp":
        return
    port, security = settings.smtp_port, settings.smtp_security
    if (port == 587 and security != "starttls") or (port == 465 and security != "ssl"):
        logger.warning(
            "SMTP_SECURITY=%s does not match SMTP_PORT=%s; use 587 with starttls or 465 with ssl", security, port
        )
    if (
        settings.smtp_host.strip().lower() == "smtp.gmail.com"
        and settings.email_from_address.strip().lower() != settings.smtp_username.strip().lower()
    ):
        logger.warning(
            "Gmail replaces the sender address with the account address: EMAIL_FROM_ADDRESS differs from SMTP_USERNAME"
        )
    if settings.app_env.strip().lower() == "production":
        logger.warning(
            "EMAIL_PROVIDER=smtp in production: some hosts block SMTP ports 25, 465 and 587 "
            "(for example Render free web services); use resend or brevo there"
        )
