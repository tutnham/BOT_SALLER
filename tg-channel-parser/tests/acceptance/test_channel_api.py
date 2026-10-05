"""Parser API acceptance: auth and pending channel create."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import API_AUTH_TOKEN

PARSER_TEST_DATABASE_URL = os.environ.get("PARSER_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(
        not PARSER_TEST_DATABASE_URL,
        reason="PARSER_TEST_DATABASE_URL not set",
    ),
]

PARSER_ROOT = Path(__file__).resolve().parents[2]


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
async def api_client(migrated_url: str) -> AsyncGenerator[AsyncClient, None]:
    from app.api.main import app
    from app.config import get_settings
    from app.db.session import dispose_engine, get_db, reset_engine_for_tests
    from tests.pg_cleanup import truncate_parser_tables

    get_settings.cache_clear()
    engine = reset_engine_for_tests(migrated_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await truncate_parser_tables(session)

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
async def test_channels_require_bearer_and_create_pending(api_client: AsyncClient) -> None:
    denied = await api_client.get("/channels")
    assert denied.status_code == 401

    created = await api_client.post(
        "/channels",
        json={"handle": "@acceptance_prices"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == "pending"
    assert body["id"]
