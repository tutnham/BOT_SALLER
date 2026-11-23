"""Tests for tg-worker transaction lifecycle."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
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
    errors.PeerIdInvalid = type("PeerIdInvalid", (Exception,), {})
    sys.modules["pyrogram"] = pyrogram
    sys.modules["pyrogram.errors"] = errors
    sys.modules["pyrogram.filters"] = pyrogram.filters
    sys.modules["pyrogram.handlers"] = pyrogram.handlers


_install_pyrogram_stub()

from pyrogram.errors import PeerIdInvalid  # noqa: E402

from app.db.models import (  # noqa: E402
    ParserChannel,
    ParserChannelPurpose,
    ParserChannelStatus,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from app.worker import task_worker  # noqa: E402


class _EmptyResult:
    """Результат select без строк (поиск дубликата channel_id не нашёл совпадений)."""

    def scalar_one_or_none(self):
        return None


class _OneResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


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

        async def execute(self, stmt):
            return _EmptyResult()

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
async def test_resolve_numeric_handle_from_title_when_channel_id_pending() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=None,
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
        username = None
        title = "Price"

    class _Client:
        async def get_chat(self, ref: object) -> _Chat:
            seen.append(ref)
            return _Chat()

    class _Session:
        async def get(self, model, ident):
            return channel

        async def execute(self, stmt):
            return _EmptyResult()

        async def flush(self) -> None:
            return None

    with patch.object(task_worker, "backfill_channel_on_activate", AsyncMock()):
        await task_worker.process_resolve_channel(_Session(), task, _Client())  # type: ignore[arg-type]

    assert seen == [-1001234567890]
    assert channel.channel_id == -1001234567890
    assert channel.status == ParserChannelStatus.active


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


@pytest.mark.asyncio
async def test_resolve_backfill_failure_enqueues_retry_task() -> None:
    """B4: сбой backfill после resolve планирует задачу backfill_channel."""
    channel = ParserChannel(
        id=1,
        channel_id=None,
        username="price",
        title="Price",
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

    class _Chat:
        id = -1001234567890
        username = "price"
        title = "Price"

    class _Client:
        async def get_chat(self, ref: object) -> _Chat:
            return _Chat()

    class _Session:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def get(self, model, ident):
            return channel

        async def execute(self, stmt):
            return _EmptyResult()

        async def flush(self) -> None:
            return None

        def add(self, obj) -> None:
            self.added.append(obj)

    session = _Session()
    with patch.object(
        task_worker,
        "backfill_channel_on_activate",
        AsyncMock(side_effect=RuntimeError("history unavailable")),
    ):
        await task_worker.process_resolve_channel(session, task, _Client())  # type: ignore[arg-type]

    # resolve остаётся успешным, канал активен
    assert task.status == ParserStatus.processed
    assert channel.status == ParserChannelStatus.active
    # создана задача повторного backfill с отложенным стартом
    assert len(session.added) == 1
    retry_task = session.added[0]
    assert isinstance(retry_task, ParserTask)
    assert retry_task.task_type == ParserTaskType.backfill_channel
    assert retry_task.channel_id == channel.id
    assert retry_task.status == ParserStatus.new
    assert retry_task.attempts == 0
    assert retry_task.scheduled_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_process_backfill_channel_success() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="price",
        title="Price",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.active,
        is_active=True,
    )
    task = ParserTask(
        id=10,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.processing,
        attempts=1,
        max_attempts=3,
    )

    class _Session:
        async def get(self, model, ident):
            return channel

        async def refresh(self, obj) -> None:
            return None

    backfill_mock = AsyncMock(return_value=5)
    with patch.object(task_worker, "backfill_channel_on_activate", backfill_mock):
        await task_worker.process_backfill_channel(_Session(), task, MagicMock())  # type: ignore[arg-type]

    backfill_mock.assert_awaited_once()
    assert task.status == ParserStatus.processed
    assert task.error_text is None


@pytest.mark.asyncio
async def test_process_backfill_channel_reruns_when_upgraded_to_full_history() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="price",
        title="Price",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.active,
        is_active=True,
    )
    task = ParserTask(
        id=11,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.processing,
        attempts=1,
        max_attempts=5,
        history_limit=None,
    )

    refresh_calls = 0

    class _Session:
        async def get(self, model, ident):
            return channel

        async def refresh(self, obj) -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls >= 2 and obj is task:
                task.history_limit = 0

    backfill_mock = AsyncMock(return_value=3)
    with patch.object(task_worker, "backfill_channel_on_activate", backfill_mock):
        await task_worker.process_backfill_channel(_Session(), task, MagicMock())  # type: ignore[arg-type]

    assert backfill_mock.await_count == 2
    assert backfill_mock.await_args_list[0].kwargs.get("limit_override") is None
    assert backfill_mock.await_args_list[1].kwargs.get("limit_override") == 0
    assert task.status == ParserStatus.processed


@pytest.mark.asyncio
async def test_process_backfill_channel_retries_with_backoff() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="price",
        title="Price",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.active,
        is_active=True,
    )
    task = ParserTask(
        id=10,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.processing,
        attempts=1,
        max_attempts=3,
    )

    class _Session:
        async def get(self, model, ident):
            return channel

        async def refresh(self, obj) -> None:
            return None

    with patch.object(
        task_worker,
        "backfill_channel_on_activate",
        AsyncMock(side_effect=RuntimeError("flood")),
    ):
        await task_worker.process_backfill_channel(_Session(), task, MagicMock())  # type: ignore[arg-type]

    assert task.status == ParserStatus.new
    assert task.scheduled_at > datetime.now(UTC)
    assert task.error_text is not None
    assert task.error_text.startswith("backfill_error:RuntimeError")


@pytest.mark.asyncio
async def test_process_backfill_channel_fails_after_max_attempts() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="price",
        title="Price",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.active,
        is_active=True,
    )
    task = ParserTask(
        id=10,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.processing,
        attempts=3,
        max_attempts=3,
    )

    class _Session:
        async def get(self, model, ident):
            return channel

        async def refresh(self, obj) -> None:
            return None

    with patch.object(
        task_worker,
        "backfill_channel_on_activate",
        AsyncMock(side_effect=RuntimeError("flood")),
    ):
        await task_worker.process_backfill_channel(_Session(), task, MagicMock())  # type: ignore[arg-type]

    assert task.status == ParserStatus.failed


@pytest.mark.asyncio
async def test_process_backfill_channel_skips_inactive_channel() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="price",
        title="Price",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.pending,
        is_active=True,
    )
    task = ParserTask(
        id=10,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.processing,
        attempts=1,
        max_attempts=3,
    )

    class _Session:
        async def get(self, model, ident):
            return channel

    backfill_mock = AsyncMock()
    with patch.object(task_worker, "backfill_channel_on_activate", backfill_mock):
        await task_worker.process_backfill_channel(_Session(), task, MagicMock())  # type: ignore[arg-type]

    backfill_mock.assert_not_awaited()
    assert task.status == ParserStatus.processed
    assert task.error_text == "channel_not_active"


@pytest.mark.asyncio
async def test_claim_next_task_picks_backfill_before_download() -> None:
    """Приоритет выборки: resolve → backfill → download."""
    task = ParserTask(
        id=11,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.new,
        attempts=0,
        max_attempts=3,
    )

    class _Result:
        def __init__(self, value) -> None:
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, stmt):
            self.calls += 1
            # первый запрос (resolve) пуст, второй (backfill) находит задачу
            return _Result(None if self.calls == 1 else task)

        async def flush(self) -> None:
            return None

    session = _Session()
    claimed = await task_worker.claim_next_task(session)  # type: ignore[arg-type]

    assert claimed is task
    assert session.calls == 2  # до download дело не дошло
    assert task.status == ParserStatus.processing
    assert task.attempts == 1


@pytest.mark.asyncio
async def test_process_claimed_task_dispatches_backfill() -> None:
    task = ParserTask(
        id=12,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.processing,
    )

    class FakeSession:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, ident):
            return task

        async def commit(self):
            self.committed = True

    fake_session = FakeSession()

    class FakeFactory:
        def __call__(self):
            return fake_session

    with patch.object(
        task_worker,
        "process_backfill_channel",
        AsyncMock(),
    ) as backfill_mock:
        with patch.object(task_worker, "get_session_factory", return_value=FakeFactory()):
            await task_worker._process_claimed_task(12, MagicMock())

    backfill_mock.assert_awaited_once()
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_resolve_positive_id_retries_with_canonical_prefix() -> None:
    """Положительный ID без -100: PeerIdInvalid → повтор с каноническим префиксом."""
    channel = ParserChannel(
        id=1,
        channel_id=None,
        username=None,
        title="1234567890",
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
        attempts=1,
        max_attempts=5,
    )
    seen: list[object] = []

    class _Chat:
        id = -1001234567890
        username = "price"
        title = "Price"

    class _Client:
        async def get_chat(self, ref: object) -> _Chat:
            seen.append(ref)
            if ref == 1234567890:
                raise PeerIdInvalid()
            return _Chat()

    class _Session:
        async def get(self, model, ident):
            return channel

        async def execute(self, stmt):
            return _EmptyResult()

        async def flush(self) -> None:
            return None

    with patch.object(task_worker, "backfill_channel_on_activate", AsyncMock()):
        await task_worker.process_resolve_channel(_Session(), task, _Client())  # type: ignore[arg-type]

    assert seen == [1234567890, -1001234567890]
    assert channel.channel_id == -1001234567890
    assert channel.status == ParserChannelStatus.active
    assert task.status == ParserStatus.processed


@pytest.mark.asyncio
async def test_resolve_non_peer_error_does_not_try_canonical() -> None:
    """Не-PeerIdInvalid ошибка не запускает эвристику -100: обычный retry."""
    channel = ParserChannel(
        id=1,
        channel_id=None,
        username=None,
        title="1234567890",
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
        attempts=1,
        max_attempts=5,
    )
    seen: list[object] = []

    class _Client:
        async def get_chat(self, ref: object):
            seen.append(ref)
            raise RuntimeError("network down")

    class _Session:
        async def get(self, model, ident):
            return channel

        async def flush(self) -> None:
            return None

    await task_worker.process_resolve_channel(_Session(), task, _Client())  # type: ignore[arg-type]

    assert seen == [1234567890]
    assert task.status == ParserStatus.new
    assert task.error_text is not None
    assert task.error_text.startswith("resolve_error:RuntimeError")
    assert channel.status == ParserChannelStatus.pending


@pytest.mark.asyncio
async def test_resolve_duplicate_channel_merges_and_reactivates_existing() -> None:
    """Коллизия UNIQUE channel_id: дубликат помечается, существующая строка реактивируется."""
    existing = ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="price",
        title="Price",
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.failed,
        is_active=False,
        error_text="deleted_by_admin",
    )
    duplicate = ParserChannel(
        id=2,
        channel_id=None,
        username="price_dup",
        title=None,
        purpose=ParserChannelPurpose.supplier_price_source,
        status=ParserChannelStatus.pending,
        is_active=True,
    )
    task = ParserTask(
        id=9,
        post_id=None,
        channel_id=2,
        task_type=ParserTaskType.resolve_channel,
        status=ParserStatus.processing,
        attempts=1,
        max_attempts=5,
    )

    class _Chat:
        id = -1001234567890
        username = "price"
        title = "Price"

    class _Client:
        async def get_chat(self, ref: object) -> _Chat:
            return _Chat()

    class _Session:
        async def get(self, model, ident):
            return duplicate

        async def execute(self, stmt):
            return _OneResult(existing)

        async def flush(self) -> None:
            return None

    backfill_mock = AsyncMock()
    with patch.object(task_worker, "backfill_channel_on_activate", backfill_mock):
        await task_worker.process_resolve_channel(_Session(), task, _Client())  # type: ignore[arg-type]

    assert duplicate.status == ParserChannelStatus.failed
    assert duplicate.error_text == "duplicate_of_channel:1"
    assert duplicate.channel_id is None
    assert task.status == ParserStatus.processed
    assert existing.is_active is True
    assert existing.status == ParserChannelStatus.active
    assert existing.error_text is None
    backfill_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_crashed_task_retries_via_fail_or_retry() -> None:
    task = ParserTask(
        id=55,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.resolve_channel,
        status=ParserStatus.processing,
        attempts=1,
        max_attempts=5,
    )

    class FakeSession:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, ident):
            return task

        async def commit(self):
            self.committed = True

    fake_session = FakeSession()

    class FakeFactory:
        def __call__(self):
            return fake_session

    with patch.object(task_worker, "get_session_factory", return_value=FakeFactory()):
        await task_worker._recover_crashed_task(55, "processor_crash:RuntimeError:boom")

    assert fake_session.committed is True
    assert task.status == ParserStatus.new
    assert task.error_text == "processor_crash:RuntimeError:boom"
    assert task.scheduled_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_poll_loop_survives_processor_crash() -> None:
    """Исключение обработчика не убивает цикл: задача восстанавливается, цикл продолжается."""

    class _Stop(BaseException):
        pass

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            return None

    fake_session = FakeSession()

    class FakeFactory:
        def __call__(self):
            return fake_session

    client = MagicMock()
    claim_mock = AsyncMock(side_effect=[55, _Stop()])
    process_mock = AsyncMock(side_effect=RuntimeError("boom"))
    recover_mock = AsyncMock()
    with (
        patch.object(task_worker, "get_session_factory", return_value=FakeFactory()),
        patch.object(task_worker, "reclaim_stale_processing", AsyncMock(return_value=0)),
        patch.object(task_worker, "_claim_next_task_id", claim_mock),
        patch.object(task_worker, "_process_claimed_task", process_mock),
        patch.object(task_worker, "_recover_crashed_task", recover_mock),
    ):
        with pytest.raises(_Stop):
            await task_worker.run_task_poll_loop(client)

    process_mock.assert_awaited_once_with(55, client)
    recover_mock.assert_awaited_once()
    recover_args = recover_mock.await_args.args
    assert recover_args[0] == 55
    assert "processor_crash:RuntimeError" in recover_args[1]
    # цикл дошёл до второй итерации (повторный claim), а не умер на первой
    assert claim_mock.await_count == 2
