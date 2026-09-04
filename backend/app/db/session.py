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
_scheduler_engine: AsyncEngine | None = None
_scheduler_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_engine(
    database_url: str,
    *,
    pool_size: int,
    max_overflow: int,
    pool_timeout: int,
    pool_recycle: int,
) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_timeout=pool_timeout,
    )


def _get_or_make_engine() -> AsyncEngine:
    """Lazy-create shared AsyncEngine (pool_pre_ping for stale connections)."""
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = _make_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
        )
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _engine


def get_engine() -> AsyncEngine:
    return _get_or_make_engine()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    _get_or_make_engine()
    assert _session_factory is not None
    return _session_factory


def get_scheduler_session_factory() -> async_sessionmaker[AsyncSession]:
    """Dedicated small pool for cron jobs so long runners do not starve web workers."""
    global _scheduler_engine, _scheduler_session_factory
    if _scheduler_engine is None:
        settings = get_settings()
        _scheduler_engine = _make_engine(
            settings.database_url,
            pool_size=settings.db_scheduler_pool_size,
            max_overflow=settings.db_scheduler_max_overflow,
            pool_timeout=settings.db_scheduler_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
        )
        _scheduler_session_factory = async_sessionmaker(
            _scheduler_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    assert _scheduler_session_factory is not None
    return _scheduler_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one AsyncSession per request."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Dispose all engines on app shutdown / test teardown."""
    global _engine, _session_factory, _scheduler_engine, _scheduler_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    if _scheduler_engine is not None:
        await _scheduler_engine.dispose()
        _scheduler_engine = None
        _scheduler_session_factory = None


def reset_engine_for_tests(database_url: str) -> AsyncEngine:
    """Rebuild engine pointing at a test DB URL (used by pytest)."""
    global _engine, _session_factory, _scheduler_engine, _scheduler_session_factory
    _engine = _make_engine(
        database_url,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    _scheduler_engine = _make_engine(
        database_url,
        pool_size=2,
        max_overflow=3,
        pool_timeout=30,
        pool_recycle=1800,
    )
    _scheduler_session_factory = async_sessionmaker(
        _scheduler_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _engine
