"""Async SQLAlchemy engine and session dependency."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from weakref import WeakSet

from sqlalchemy import Engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Sync engines whose transactions get the statement and lock timeouts. Only the application engine is
# added by default; a test engine gets them only when a test calls ``enable_transaction_timeouts``.
_TIMEOUT_ENGINES: WeakSet[Engine] = WeakSet()


def _set_transaction_timeouts(connection: Connection) -> None:
    """Engine ``begin`` listener: bound every statement and lock wait in this transaction.

    Supabase's transaction pooler (port 6543) does not support startup parameters such as
    ``server_settings={"statement_timeout": ...}``, and a plain ``SET`` would leak to whichever client
    gets the server connection next. ``SET LOCAL`` lasts only until the end of the current transaction,
    so it is issued at the start of every transaction. SQLAlchemy suppresses autobegin while this event
    runs, so executing here cannot recurse. If this ever conflicts with the pooler, set both
    ``DB_STATEMENT_TIMEOUT_MS`` and ``DB_LOCK_TIMEOUT_MS`` to 0 to disable the feature entirely.
    """
    if connection.engine not in _TIMEOUT_ENGINES:
        return
    settings = get_settings()
    # Values are validated integers (0 or 100..600000); SET cannot take bind parameters.
    if settings.db_statement_timeout_ms > 0:
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {int(settings.db_statement_timeout_ms)}")
    if settings.db_lock_timeout_ms > 0:
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = {int(settings.db_lock_timeout_ms)}")


def enable_transaction_timeouts(engine: AsyncEngine) -> None:
    """Apply ``SET LOCAL statement_timeout`` and ``lock_timeout`` at the start of each transaction."""
    sync_engine = engine.sync_engine
    _TIMEOUT_ENGINES.add(sync_engine)
    if not event.contains(sync_engine, "begin", _set_transaction_timeouts):
        event.listen(sync_engine, "begin", _set_transaction_timeouts)


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
        enable_transaction_timeouts(_engine)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Create and cache a session factory without connecting to the database."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


def get_session_factory() -> Callable[[], AsyncSession]:
    """FastAPI dependency returning a factory for short, independent transactions (payment code).

    The engine is only created when the factory is first called, so routes that never use it work
    without a database. Tests substitute this dependency.
    """

    def factory() -> AsyncSession:
        return get_sessionmaker()()

    return factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a session, creating the engine lazily on first use."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run operations in an explicit database transaction."""
    async with session.begin():
        yield session
