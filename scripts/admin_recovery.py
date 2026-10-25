"""Operator recovery for administrator accounts, for when email delivery is down.

Usage (from the repository root, with ``.env`` pointing at the database; the operator already holds
database access, so this grants nothing new):

    ALLOW_ADMIN_RECOVERY=1 python -m scripts.admin_recovery reset-password --email admin@example.com
    ALLOW_ADMIN_RECOVERY=1 python -m scripts.admin_recovery issue-login-code --email admin@example.com

On PowerShell set the variable first: ``$env:ALLOW_ADMIN_RECOVERY = "1"``.

- ``reset-password`` asks twice for a new password (at least 12 characters, the admin password policy),
  stores its hash, sets ``must_change_password`` and ``password_changed_at`` (every earlier access token
  stops working). The administrator then signs in with it and must change it.
- ``issue-login-code`` creates a login challenge for an enabled administrator exactly like the sign in
  endpoint does (it supersedes the active challenge; the hourly challenge limit is not applied) and
  prints the challenge token and the code once. Enter them with ``POST /api/v1/auth/login/verify``.

Both commands refuse to run unless ``ALLOW_ADMIN_RECOVERY=1``, work only for administrator accounts and
never accept a password or code on the command line. Nothing secret is logged; the token and code are
printed to this terminal only, once.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from getpass import getpass

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select

from app.core.codes import format_code
from app.core.errors import AppError
from app.core.password_policy import validate_new_password
from app.core.security import hash_password
from app.db import get_sessionmaker
from app.integrations.email import mask_email
from app.models.enums import UserRole
from app.models.user import User
from app.services import auth_service

ENABLE_VARIABLE = "ALLOW_ADMIN_RECOVERY"


def recovery_allowed(environ: dict[str, str] | None = None) -> bool:
    return (os.environ if environ is None else environ).get(ENABLE_VARIABLE, "").strip() == "1"


def normalized_email(raw: str) -> str:
    try:
        return str(TypeAdapter(EmailStr).validate_python(raw.strip())).lower()
    except ValidationError:
        raise SystemExit("That is not a valid email address.") from None


async def find_admin(email: str) -> User:
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or user.role != UserRole.ADMIN:
        # The same answer for an unknown address and a non administrator.
        raise SystemExit("No administrator account has that email address.")
    return user


async def reset_password(email: str) -> None:
    admin = await find_admin(email)
    password = await asyncio.to_thread(getpass, "New password (at least 12 characters): ")
    confirmation = await asyncio.to_thread(getpass, "Repeat the new password: ")
    if password != confirmation:
        raise SystemExit("The passwords did not match; nothing was changed.")
    try:
        validate_new_password(password, UserRole.ADMIN, email=admin.email, name=admin.name)
    except AppError as exc:
        raise SystemExit(f"The password is too weak: {exc.reason}") from None
    password_hash = hash_password(password)
    del password, confirmation
    async with get_sessionmaker()() as session:
        target = await session.scalar(select(User).where(User.id == admin.id).with_for_update())
        if target is None:
            raise SystemExit("No administrator account has that email address.")
        target.password_hash = password_hash
        target.must_change_password = True
        target.password_changed_at = func.now()
        await session.commit()
    print(f"Password reset for {mask_email(admin.email)}. It must be changed at the next sign in.")
    print("Every earlier access token for this account no longer works.")


async def issue_login_code(email: str) -> None:
    admin = await find_admin(email)
    if admin.is_disabled:
        raise SystemExit("That administrator account is disabled; enable it first.")
    async with get_sessionmaker()() as session:
        issued = await auth_service.issue_login_challenge(session, admin, enforce_hourly_limit=False)
    print("WARNING: use this only when email delivery is down. You already hold database access;")
    print("treat the values below like a password, use them once and do not store or share them.")
    print(f"Account:         {mask_email(admin.email)}")
    print(f"Challenge token: {issued.token}")
    print(f"Code:            {format_code(issued.code)}")
    print(f"Code expires at: {issued.code_expires_at.isoformat()}")
    print("Enter both with POST /api/v1/auth/login/verify (or the sign in screen's code step).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Administrator recovery (operator tool).")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("reset-password", "set a new password for an administrator (asked twice, never on the command line)"),
        ("issue-login-code", "print a sign in challenge token and code for an enabled administrator"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--email", required=True, help="the administrator's email address")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not recovery_allowed():
        print(f"Refusing to run: set {ENABLE_VARIABLE}=1 to use the recovery tool.", file=sys.stderr)
        return 2
    email = normalized_email(args.email)
    if args.command == "reset-password":
        asyncio.run(reset_password(email))
    else:
        asyncio.run(issue_login_code(email))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
