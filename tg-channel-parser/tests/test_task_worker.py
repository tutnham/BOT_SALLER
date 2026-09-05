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

from app.db.models import ParserStatus, ParserTask, ParserTaskType  # noqa: E402
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
