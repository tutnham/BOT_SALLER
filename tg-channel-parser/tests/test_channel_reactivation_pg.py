"""Integration: реактивация мягко удалённых каналов и коллизия channel_id на реальном PG."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
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
async def react_client(migrated_url: str) -> AsyncGenerator[AsyncClient, None]:
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

    async with factory() as session:
        from tests.pg_cleanup import truncate_parser_tables

        await truncate_parser_tables(session)

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_upsert_channel_restores_is_active_on_real_pg(
    react_client: AsyncClient,
    migrated_url: str,
) -> None:
    """CLI add_channel поверх мягко удалённой строки возвращает канал в список и листнер."""
    from app.db.models import ParserChannel, ParserChannelPurpose, ParserChannelStatus
    from app.db.session import get_session_factory
    from app.mtproto.upsert import upsert_channel

    factory = get_session_factory()
    async with factory() as session:
        row = ParserChannel(
            channel_id=-100900001,
            username="old_prices",
            title="Old Prices",
            purpose=ParserChannelPurpose.supplier_price_source,
            status=ParserChannelStatus.failed,
            is_active=False,
            error_text="deleted_by_admin",
        )
        session.add(row)
        await session.commit()
        pk = row.id

    async with factory() as session:
        await upsert_channel(
            session,
            channel_id=-100900001,
            username="old_prices",
            title="Old Prices",
            purpose=ParserChannelPurpose.supplier_price_source,
        )
        await session.commit()

    engine = create_async_engine(migrated_url, poolclass=NullPool)
    check_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with check_factory() as session:
            refreshed = await session.get(ParserChannel, pk)
        assert refreshed is not None
        assert refreshed.is_active is True
        assert refreshed.status == ParserChannelStatus.active
        assert refreshed.error_text is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_readd_deleted_channel_reactivates_same_row(
    react_client: AsyncClient,
) -> None:
    """Повторный POST /channels того же username реактивирует мягко удалённую строку."""
    from tests.conftest import API_AUTH_TOKEN

    headers = {"Authorization": f"Bearer {API_AUTH_TOKEN}"}
    resp = await react_client.post(
        "/channels", json={"handle": "@restore_prices"}, headers=headers
    )
    assert resp.status_code == 202
    pk = resp.json()["id"]

    resp = await react_client.delete(f"/channels/{pk}", headers=headers)
    assert resp.status_code == 200

    resp = await react_client.get("/channels", headers=headers)
    deleted = next(ch for ch in resp.json() if ch["id"] == pk)
    assert deleted["is_active"] is False

    resp = await react_client.post(
        "/channels", json={"handle": "@restore_prices"}, headers=headers
    )
    assert resp.status_code == 202
    restored = resp.json()
    assert restored["id"] == pk
    assert restored["is_active"] is True
    assert restored["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_duplicate_does_not_violate_unique_channel_id(
    react_client: AsyncClient,
    migrated_url: str,
) -> None:
    """Две строки одного канала: вторая resolve-задача не роняет воркер на UNIQUE."""
    from app.db.models import (
        ParserChannel,
        ParserChannelPurpose,
        ParserChannelStatus,
        ParserStatus,
        ParserTask,
        ParserTaskType,
    )
    from app.db.session import get_session_factory
    from app.worker.task_worker import process_resolve_channel

    factory = get_session_factory()
    async with factory() as session:
        existing = ParserChannel(
            channel_id=-100900002,
            username="dup_primary",
            title="Dup Primary",
            purpose=ParserChannelPurpose.supplier_price_source,
            status=ParserChannelStatus.failed,
            is_active=False,
            error_text="deleted_by_admin",
        )
        duplicate = ParserChannel(
            channel_id=None,
            username="dup_secondary",
            title=None,
            purpose=ParserChannelPurpose.supplier_price_source,
            status=ParserChannelStatus.pending,
            is_active=True,
        )
        session.add_all([existing, duplicate])
        await session.flush()
        task = ParserTask(
            post_id=None,
            channel_id=duplicate.id,
            task_type=ParserTaskType.resolve_channel,
            status=ParserStatus.processing,
            attempts=1,
            max_attempts=5,
        )
        session.add(task)
        await session.commit()
        existing_pk, duplicate_pk, task_pk = existing.id, duplicate.id, task.id

    class _Client:
        async def get_chat(self, ref: object) -> SimpleNamespace:
            return SimpleNamespace(id=-100900002, username="dup_primary", title="Dup Primary")

    async with factory() as session:
        task_row = await session.get(ParserTask, task_pk)
        assert task_row is not None
        await process_resolve_channel(session, task_row, _Client())
        await session.commit()

    engine = create_async_engine(migrated_url, poolclass=NullPool)
    check_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with check_factory() as session:
            existing_row = await session.get(ParserChannel, existing_pk)
            duplicate_row = await session.get(ParserChannel, duplicate_pk)
            task_row = await session.get(ParserTask, task_pk)
        assert existing_row is not None
        assert existing_row.is_active is True
        assert existing_row.status == ParserChannelStatus.active
        assert duplicate_row is not None
        assert duplicate_row.status == ParserChannelStatus.failed
        assert duplicate_row.error_text == f"duplicate_of_channel:{existing_pk}"
        assert duplicate_row.channel_id is None
        assert task_row is not None
        assert task_row.status == ParserStatus.processed
    finally:
        await engine.dispose()