"""Run manually once per environment to create the single super administrator."""

import asyncio
from getpass import getpass

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import get_sessionmaker
from app.models.enums import UserRole
from app.models.user import User
from app.utils.phone import normalize_phone


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
    if not settings.seed_admin_phone:
        raise ValueError("SEED_ADMIN_PHONE is required")
    phone = normalize_phone(settings.seed_admin_phone)
    if not settings.seed_admin_email:
        raise ValueError("SEED_ADMIN_EMAIL is required for the super admin")
    email = str(TypeAdapter(EmailStr).validate_python(settings.seed_admin_email)).lower()

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
        phone_number=phone,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.ADMIN,
        is_super_admin=True,
        must_change_password=True,
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
    print(f"Phone: {admin.phone_number}")
    print("Remove SEED_ADMIN_PASSWORD from .env after the first run.")


if __name__ == "__main__":
    asyncio.run(seed_admin())
