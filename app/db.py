"""Async SQLAlchemy engine and session dependency."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()
database_url = settings.database_url
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = (
    create_async_engine(
        database_url,
        connect_args={"statement_cache_size": 0},
        pool_pre_ping=True,
    )
    if database_url
    else None
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a database session, failing clearly when configuration is missing."""
    if engine is None:
        raise RuntimeError("DATABASE_URL must be configured before database access")
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run operations in an explicit database transaction."""
    async with session.begin():
        yield session
