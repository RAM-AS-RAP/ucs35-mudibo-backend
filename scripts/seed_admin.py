"""Run manually once per environment to create the single super administrator."""

import asyncio
from getpass import getpass

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import SessionLocal
from app.models.enums import UserRole
from app.models.user import User
from app.utils.phone import normalize_phone


async def seed_admin() -> None:
    """Create the configured initial admin, or leave an existing super admin untouched."""
    settings = get_settings()
    async with SessionLocal() as session:
        existing = await session.scalar(select(User).where(User.is_super_admin.is_(True)))
        if existing is not None:
            print("A super admin already exists; no changes made.")
            return
    name = settings.seed_admin_name or input("Admin name: ").strip()
    phone = normalize_phone(settings.seed_admin_phone or input("Admin phone: ").strip())
    email = settings.seed_admin_email or input("Admin email (optional): ").strip() or None
    password = settings.seed_admin_password or getpass("Initial admin password: ")
    if len(password) < 8:
        raise ValueError("Admin password must be at least 8 characters")
    async with SessionLocal() as session:
        session.add(
            User(
                name=name,
                phone_number=phone,
                email=email,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                is_super_admin=True,
                must_change_password=True,
            )
        )
        await session.commit()
    del password
    print("Super admin created.")


if __name__ == "__main__":
    asyncio.run(seed_admin())
