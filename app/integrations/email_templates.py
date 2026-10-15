"""Invite and password reset emails (English, plain text plus a simple HTML version).

The code appears only in the body, never in the subject or in a link. No email contains a password.
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
