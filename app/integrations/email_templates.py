"""Invite, password reset, registration and sign in emails (English, plain text plus simple HTML).

The code appears only in the body, never in the subject or in a link. No email contains a password
or a link carrying a secret.
"""

from html import escape

from app.core.codes import format_code
from app.core.config import get_settings
from app.integrations.email import EmailMessage

IGNORE_LINE = "If you did not expect this email, ignore it."


def _page(path: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}{path}"


def _html(paragraphs: list[str], code: str) -> str:
    body = "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
    code_block = (
        f'<p style="font-size:22px;font-family:monospace;letter-spacing:2px;font-weight:bold">{escape(code)}</p>'
    )
    return f"<!doctype html><html><body>{code_block}{body}</body></html>"


def invite_email(to: str, inviter_name: str, code: str, minutes: int) -> EmailMessage:
    formatted = format_code(code)
    page = _page("/accept-invite")
    lines = [
        f"You have been invited as an administrator of AgriSence by {inviter_name}.",
        f"Your code is {formatted}. It is valid for {minutes} minutes.",
        f"Enter it at {page} with your email address and choose your password.",
        IGNORE_LINE,
    ]
    return EmailMessage(
        to=to,
        subject="Your AgriSence administrator invitation",
        text="\n\n".join(lines) + "\n",
        html=_html(lines, formatted),
    )


def reset_email(to: str, code: str, minutes: int) -> EmailMessage:
    formatted = format_code(code)
    page = _page("/reset-password")
    lines = [
        "A password reset was requested for your AgriSence account.",
        f"Your code is {formatted}. It is valid for {minutes} minutes.",
        f"Enter it at {page} with your email address and choose a new password.",
        IGNORE_LINE,
    ]
    return EmailMessage(
        to=to,
        subject="Your AgriSence password reset code",
        text="\n\n".join(lines) + "\n",
        html=_html(lines, formatted),
    )


def _plain_html(paragraphs: list[str]) -> str:
    body = "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
    return f"<!doctype html><html><body>{body}</body></html>"


def registration_email(to: str, code: str, minutes: int) -> EmailMessage:
    """The code that confirms a farmer or buyer registration. No link: the code is typed in."""
    formatted = format_code(code)
    lines = [
        "Confirm your email for AgriSence.",
        f"Your code is {formatted}. It is valid for {minutes} minutes.",
        "Enter it on the registration page.",
        "If you did not try to register, ignore this email.",
    ]
    return EmailMessage(
        to=to,
        subject="Confirm your email for AgriSence",
        text="\n\n".join(lines) + "\n",
        html=_html(lines, formatted),
    )


def account_exists_email(to: str) -> EmailMessage:
    """Sent instead of a code when someone registers with an address that already has an account."""
    lines = [
        "Someone tried to register with this email address, but an account already exists.",
        "If it was you, sign in, or use Forgot password if you do not remember your password.",
        "If it was not you, you can ignore this email.",
    ]
    return EmailMessage(
        to=to,
        subject="Your AgriSence account",
        text="\n\n".join(lines) + "\n",
        html=_plain_html(lines),
    )


def login_code_email(to: str, code: str, minutes: int) -> EmailMessage:
    """The second sign in step; sent only after the password was verified."""
    formatted = format_code(code)
    lines = [
        f"Your AgriSence sign in code is {formatted}. It is valid for {minutes} minutes.",
        "If you did not just try to sign in, change your password.",
    ]
    return EmailMessage(
        to=to,
        subject="Your AgriSence sign in code",
        text="\n\n".join(lines) + "\n",
        html=_html(lines, formatted),
    )


def check_email(to: str) -> EmailMessage:
    """The message sent by ``POST /admin/email/test``."""
    text = (
        "This is a test email from the AgriSence backend. Email sending works.\n\n"
        "It contains no code and needs no action.\n"
    )
    return EmailMessage(
        to=to,
        subject="AgriSence email test",
        text=text,
        html="<!doctype html><html><body><p>This is a test email from the AgriSence backend. "
        "Email sending works.</p><p>It contains no code and needs no action.</p></body></html>",
    )
