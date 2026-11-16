"""B1 PostgreSQL: edit обновляет прайс в GET /posts, delete исключает пост."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
async def edit_env(
    migrated_url: str,
) -> AsyncGenerator[tuple[AsyncClient, async_sessionmaker[AsyncSession]], None]:
    from app.api.main import app
    from app.config import get_settings
    from app.db.session import dispose_engine, get_db, reset_engine_for_tests
    from tests.pg_cleanup import truncate_parser_tables

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
        await truncate_parser_tables(session)

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory
    app.dependency_overrides.clear()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_post(factory: async_sessionmaker[AsyncSession], *, channel_id: int) -> None:
    from app.db.models import ParserChannelPurpose
    from app.mtproto.upsert import upsert_channel, upsert_post
    from tests.conftest import make_message

    async with factory() as session:
        await upsert_channel(
            session,
            channel_id=channel_id,
            username="edit_delete_prices",
            title="Edit/Delete prices",
            purpose=ParserChannelPurpose.supplier_price_source,
        )
        await upsert_post(
            session,
            make_message(chat_id=channel_id, message_id=10, text="iPhone 15 70000"),
        )
        await session.commit()


async def _get_posts(client: AsyncClient, channel_id: int) -> list[dict]:
    from tests.conftest import API_AUTH_TOKEN

    resp = await client.get(
        "/posts",
        params={"channel_id": channel_id, "content_type": "text"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert resp.status_code == 200
    return resp.json()["items"]


@pytest.mark.asyncio
async def test_edit_updates_price_and_repeat_edit_idempotent(
    edit_env: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    from app.mtproto.upsert import apply_post_edit
    from tests.conftest import make_message

    client, factory = edit_env
    channel_id = -100901
    await _seed_post(factory, channel_id=channel_id)

    edit_date = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    async with factory() as session:
        post = await apply_post_edit(
            session,
            make_message(
                chat_id=channel_id,
                message_id=10,
                text="iPhone 15 65000",
                edit_date=edit_date,
            ),
        )
        await session.commit()
        assert post is not None
        assert post.edited_at == edit_date
        original_date = post.post_date

    items = await _get_posts(client, channel_id)
    assert len(items) == 1
    assert items[0]["raw_text"] == "iPhone 15 65000"

    # Повторная доставка того же редактирования — no-op
    async with factory() as session:
        post = await apply_post_edit(
            session,
            make_message(
                chat_id=channel_id,
                message_id=10,
                text="подмена задним числом",
                edit_date=edit_date,
            ),
        )
        await session.commit()
        assert post is not None
        assert post.raw_text == "iPhone 15 65000"
        assert post.post_date == original_date

    items = await _get_posts(client, channel_id)
    assert items[0]["raw_text"] == "iPhone 15 65000"


@pytest.mark.asyncio
async def test_deleted_post_excluded_from_posts_api(
    edit_env: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    from app.mtproto.upsert import mark_posts_deleted

    client, factory = edit_env
    channel_id = -100902
    await _seed_post(factory, channel_id=channel_id)

    async with factory() as session:
        count = await mark_posts_deleted(session, channel_id=channel_id, message_ids=[10])
        await session.commit()
        assert count == 1

    assert await _get_posts(client, channel_id) == []

    # Повторное удаление — идемпотентно
    async with factory() as session:
        count = await mark_posts_deleted(session, channel_id=channel_id, message_ids=[10])
        await session.commit()
        assert count == 0