"""Run manually once per environment to create the single super administrator.

Reads ``SEED_ADMIN_NAME``, ``SEED_ADMIN_EMAIL`` and ``SEED_ADMIN_PASSWORD`` (or asks for the password
twice; at least 12 characters). The account is created with ``must_change_password`` set and a
verified email. Running it again when a super admin exists changes nothing. The password is never
printed.

The first sign in needs the emailed sign in code (admins are always in ``LOGIN_OTP_ROLES`` in
production), so email delivery must work first. With ``EMAIL_PROVIDER=console`` the code is written
to ``.dev_mailbox/``. If email is down, an operator with database access can use
``python -m scripts.admin_recovery issue-login-code --email <email>``.
"""

import asyncio
from getpass import getpass

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import get_sessionmaker
from app.integrations.email import mask_email
from app.models.enums import UserRole
from app.models.user import User


async def seed_admin() -> None:
    """Create the first super admin; run once manually in each environment."""
    settings = get_settings()
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        existing = await session.scalar(select(User).where(User.is_super_admin.is_(True)))
        if existing is not None:
            print("A super admin already exists; no changes made.")
            return
    name = settings.seed_admin_name.strip()
    if not name:
        raise ValueError("SEED_ADMIN_NAME is required")
    if not settings.seed_admin_email:
        raise ValueError("SEED_ADMIN_EMAIL is required for the super admin")
    email = str(TypeAdapter(EmailStr).validate_python(settings.seed_admin_email.strip())).lower()

    password = settings.seed_admin_password
    if not password:
        password = await asyncio.to_thread(getpass, "Initial admin password: ")
        confirmation = await asyncio.to_thread(getpass, "Confirm initial admin password: ")
        if password != confirmation:
            raise ValueError("Passwords did not match")
    if len(password) < 12:
        raise ValueError("SEED_ADMIN_PASSWORD must contain at least 12 characters")

    admin = User(
        name=name,
        email=email,
        email_verified_at=func.now(),
        password_hash=hash_password(password),
        role=UserRole.ADMIN,
        is_super_admin=True,
        must_change_password=True,
        password_changed_at=func.now(),
    )
    try:
        async with session_factory() as session:
            session.add(admin)
            await session.commit()
            await session.refresh(admin)
    except IntegrityError:
        async with session_factory() as session:
            existing = await session.scalar(select(User).where(User.is_super_admin.is_(True)))
        if existing is None:
            raise
        print("A super admin already exists; no changes made.")
        return
    finally:
        del password

    print(f"Super admin ID: {admin.id}")
    print(f"Name: {admin.name}")
    print(f"Email: {mask_email(admin.email)}")
    print("Remove SEED_ADMIN_PASSWORD from .env after the first run.")
    print(
        "The first sign in needs the emailed sign in code: make sure email delivery works "
        "(EMAIL_PROVIDER=console writes it to .dev_mailbox/), or use scripts/admin_recovery.py issue-login-code."
    )


if __name__ == "__main__":
    asyncio.run(seed_admin())
