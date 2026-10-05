"""PostgreSQL integration tests (SKIP LOCKED, pending username unique)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    ParserChannel,
    ParserChannelStatus,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from app.db.session import reset_engine_for_tests
from app.worker.task_worker import claim_next_task
from tests.pg_cleanup import truncate_parser_tables

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
async def pg_factory(migrated_url: str) -> async_sessionmaker[AsyncSession]:
    reset_engine_for_tests(migrated_url)
    engine = create_async_engine(migrated_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await truncate_parser_tables(session)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_next_task_skip_locked(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_factory() as session:
        channel = ParserChannel(
            channel_id=None,
            username="skip_locked_test",
            title="skip",
            status=ParserChannelStatus.pending,
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        task = ParserTask(
            post_id=None,
            channel_id=channel.id,
            task_type=ParserTaskType.resolve_channel,
            status=ParserStatus.new,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    async def claim_once() -> int | None:
        async with pg_factory() as session:
            claimed = await claim_next_task(session)
            if claimed is None:
                await session.rollback()
                return None
            claimed_id = claimed.id
            await session.commit()
            return claimed_id

    first, second = await asyncio.gather(claim_once(), claim_once())
    claimed_ids = {value for value in (first, second) if value is not None}
    assert claimed_ids == {task_id}


@pytest.mark.asyncio
async def test_pending_username_unique_index_enforced(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_factory() as session:
        session.add(
            ParserChannel(
                channel_id=None,
                username="UniquePending",
                title="a",
                status=ParserChannelStatus.pending,
                is_active=True,
            )
        )
        await session.commit()

    async with pg_factory() as session:
        session.add(
            ParserChannel(
                channel_id=None,
                username="uniquepending",
                title="b",
                status=ParserChannelStatus.pending,
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with pg_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ParserChannel).where(
                        ParserChannel.status == ParserChannelStatus.pending,
                        ParserChannel.username.ilike("uniquepending"),
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
