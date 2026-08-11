"""Async SQLAlchemy engine and FastAPI session dependency."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_engine(database_url: str) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle,
        pool_timeout=settings.db_pool_timeout,
    )


def get_engine() -> AsyncEngine:
    """Lazy-create shared AsyncEngine (pool_pre_ping for stale connections)."""
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = _make_engine(settings.database_url)
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one AsyncSession per request."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Dispose engine on app shutdown / test teardown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def reset_engine_for_tests(database_url: str) -> AsyncEngine:
    """Rebuild engine pointing at a test DB URL (used by pytest)."""
    global _engine, _session_factory
    _engine = _make_engine(database_url)
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _engine
