"""Async SQLAlchemy engine and session dependency."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Create and cache the async engine on first database use."""
    global _engine
    if _engine is None:
        database_url = get_settings().database_url
        if not database_url:
            raise RuntimeError("DATABASE_URL must be configured before database access")
        if database_url.startswith(("postgresql://", "postgres://")):
            database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        _engine = create_async_engine(
            database_url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"statement_cache_size": 0},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Create and cache a session factory without connecting to the database."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a session, creating the engine lazily on first use."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run operations in an explicit database transaction."""
    async with session.begin():
        yield session
