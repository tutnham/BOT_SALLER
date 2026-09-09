"""Integration: POST /channels persists after get_db commit."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

PARSER_TEST_DATABASE_URL = os.environ.get("PARSER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not PARSER_TEST_DATABASE_URL,
    reason="PARSER_TEST_DATABASE_URL not set",
)

PARSER_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def migrated_url() -> str:
    assert PARSER_TEST_DATABASE_URL is not None
    os.environ["DATABASE_URL"] = PARSER_TEST_DATABASE_URL
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = Config(str(PARSER_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PARSER_ROOT / "app" / "migrations"))
    command.upgrade(cfg, "head")
    return PARSER_TEST_DATABASE_URL


@pytest_asyncio.fixture
async def persist_client(migrated_url: str) -> AsyncGenerator[AsyncClient, None]:
    from app.api.main import app
    from app.config import get_settings
    from app.db.session import dispose_engine, get_db, reset_engine_for_tests

    get_settings.cache_clear()
    engine = reset_engine_for_tests(migrated_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_create_channel_survives_new_session(
    persist_client: AsyncClient,
    migrated_url: str,
) -> None:
    from app.db.models import ParserChannel, ParserTask
    from tests.conftest import API_AUTH_TOKEN

    response = await persist_client.post(
        "/channels",
        json={"handle": "@persist_prices"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert response.status_code == 202
    channel_id = response.json()["id"]

    engine = create_async_engine(migrated_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            channel = await session.get(ParserChannel, channel_id)
            tasks = list(
                (
                    await session.execute(
                        select(ParserTask).where(ParserTask.channel_id == channel_id)
                    )
                ).scalars().all()
            )
        assert channel is not None
        assert channel.username == "persist_prices"
        assert len(tasks) == 1
    finally:
        await engine.dispose()
