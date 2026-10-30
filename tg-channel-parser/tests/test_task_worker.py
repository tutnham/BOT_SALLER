"""Tests for tg-worker transaction lifecycle."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _install_pyrogram_stub() -> None:
    if "pyrogram" in sys.modules:
        return
    pyrogram = ModuleType("pyrogram")
    pyrogram.Client = MagicMock()
    pyrogram.filters = MagicMock()
    pyrogram.handlers = MagicMock()
    errors = ModuleType("pyrogram.errors")
    errors.FloodWait = Exception
    errors.SessionPasswordNeeded = Exception
    sys.modules["pyrogram"] = pyrogram
    sys.modules["pyrogram.errors"] = errors
    sys.modules["pyrogram.filters"] = pyrogram.filters
    sys.modules["pyrogram.handlers"] = pyrogram.handlers


_install_pyrogram_stub()

from app.db.models import (  # noqa: E402
    ParserChannel,
    ParserChannelPurpose,
    ParserChannelStatus,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from app.worker import task_worker  # noqa: E402


@pytest.mark.asyncio
async def test_claim_next_task_id_commits_before_return() -> None:
    task = ParserTask(
        id=42,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.resolve_channel,
        status=ParserStatus.processing,
    )

    class FakeSession:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            self.committed = True

    fake_session = FakeSession()

    class FakeFactory:
        def __call__(self):
            return fake_session

    with patch.object(task_worker, "claim_next_task", AsyncMock(return_value=task)):
        with patch.object(task_worker, "get_session_factory", return_value=FakeFactory()):
            task_id = await task_worker._claim_next_task_id()

    assert task_id == 42
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_process_claimed_task_uses_fresh_session() -> None:
    task = ParserTask(
        id=7,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.resolve_channel,
        status=ParserStatus.processing,
    )
    client = MagicMock()

    class FakeSession:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, ident):
            assert model is ParserTask
            assert ident == 7
            return task

        async def commit(self):
            self.committed = True

    fake_session = FakeSession()

    class FakeFactory:
        def __call__(self):
            return fake_session

    with patch.object(
        task_worker,
        "process_resolve_channel",
        AsyncMock(),
    ) as resolve_mock:
        with patch.object(task_worker, "get_session_factory", return_value=FakeFactory()):
            await task_worker._process_claimed_task(7, client)

    resolve_mock.assert_awaited_once()
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_resolve_numeric_channel_passes_int_to_get_chat() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username=None,
        title="-1001234567890",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.pending,
        is_active=True,
    )
    task = ParserTask(
        id=9,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.resolve_channel,
        status=ParserStatus.processing,
    )
    seen: list[object] = []

    class _Chat:
        id = -1001234567890
        username = "price"
        title = "Price"

    class _Client:
        async def get_chat(self, ref: object) -> _Chat:
            seen.append(ref)
            return _Chat()

    class _Session:
        async def get(self, model, ident):
            assert model is ParserChannel
            assert ident == 1
            return channel

        async def flush(self) -> None:
            return None

    with patch.object(task_worker, "backfill_channel_on_activate", AsyncMock()):
        await task_worker.process_resolve_channel(_Session(), task, _Client())  # type: ignore[arg-type]

    assert seen == [-1001234567890]
    assert isinstance(seen[0], int)
    assert channel.status == ParserChannelStatus.active
    assert channel.title == "Price"
    assert channel.username == "price"
    assert task.status == ParserStatus.processed


@pytest.mark.asyncio
async def test_reclaim_stale_processing_updates_stuck_tasks() -> None:
    class FakeResult:
        rowcount = 3

    class FakeSession:
        def __init__(self) -> None:
            self.stmt = None

        async def execute(self, stmt):
            self.stmt = stmt
            return FakeResult()

    session = FakeSession()
    count = await task_worker.reclaim_stale_processing(session)  # type: ignore[arg-type]
    assert count == 3
    assert session.stmt is not None
