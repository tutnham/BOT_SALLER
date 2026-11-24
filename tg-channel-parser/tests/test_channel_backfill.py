"""Bounded backfill after channel activate."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import ParserChannel, ParserChannelStatus
from app.mtproto.channel_backfill import backfill_channel_on_activate


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def scalar(self, stmt: Any) -> int | None:
        return 99

    async def execute(self, stmt: Any) -> None:
        return None


class _FakeFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


@pytest.mark.asyncio
async def test_backfill_stops_on_scanned_limit_without_offset_id() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-100123,
        status=ParserChannelStatus.active,
        is_active=True,
        last_synced_message_id=50,
    )
    seen_kwargs: dict[str, Any] = {}
    scanned: list[int] = []

    class _Client:
        def get_chat_history(self, **kwargs: Any):
            seen_kwargs.update(kwargs)

            async def _gen():
                for message_id in range(1, 20):
                    yield SimpleNamespace(
                        id=message_id,
                        date=datetime(2026, 1, 1, tzinfo=UTC),
                    )

            return _gen()

    async def _upsert(_session: Any, message: Any) -> None:
        scanned.append(message.id)
        return None

    settings = SimpleNamespace(backfill_on_activate_limit=3)
    with (
        patch("app.mtproto.channel_backfill.get_session_factory", return_value=_FakeFactory()),
        patch("app.mtproto.channel_backfill.upsert_post", new=AsyncMock(side_effect=_upsert)),
    ):
        inserted = await backfill_channel_on_activate(
            _Client(),
            channel=channel,
            settings=settings,  # type: ignore[arg-type]
        )

    assert inserted == 0
    assert scanned == [1, 2, 3]
    assert seen_kwargs == {"chat_id": -100123, "limit": 3}
    assert "offset_id" not in seen_kwargs


@pytest.mark.asyncio
async def test_backfill_limit_override_zero_scans_full_history() -> None:
    channel = ParserChannel(
        id=1,
        channel_id=-100123,
        status=ParserChannelStatus.active,
        is_active=True,
        last_synced_message_id=50,
    )
    scanned: list[int] = []

    class _Client:
        def get_chat_history(self, **kwargs: Any):
            async def _gen():
                for message_id in range(1, 19):
                    yield SimpleNamespace(
                        id=message_id,
                        date=datetime(2026, 1, 1, tzinfo=UTC),
                    )

            return _gen()

    async def _upsert(_session: Any, message: Any) -> None:
        scanned.append(message.id)
        return None

    settings = SimpleNamespace(backfill_on_activate_limit=3)
    with (
        patch("app.mtproto.channel_backfill.get_session_factory", return_value=_FakeFactory()),
        patch("app.mtproto.channel_backfill.upsert_post", new=AsyncMock(side_effect=_upsert)),
    ):
        await backfill_channel_on_activate(
            _Client(),
            channel=channel,
            settings=settings,  # type: ignore[arg-type]
            limit_override=0,
        )

    assert scanned == list(range(1, 19))
