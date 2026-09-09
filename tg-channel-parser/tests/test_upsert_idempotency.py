"""upsert_post idempotency: second insert is no-op, no duplicate tasks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.db.models import (
    ParserChannelPurpose,
    ParserContentType,
)
from app.mtproto.upsert import upsert_post
from tests.conftest import make_message


class _ReturningResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value


@pytest.mark.asyncio
async def test_upsert_skips_unregistered_channel() -> None:
    session = AsyncMock()
    # get_channel returns None
    session.execute = AsyncMock(return_value=_ReturningResult(None))

    msg = make_message()
    result = await upsert_post(session, msg)
    assert result is None


@pytest.mark.asyncio
async def test_upsert_idempotent_conflict_returns_none() -> None:
    channel = SimpleNamespace(
        channel_id=-100123,
        purpose=ParserChannelPurpose.supplier_price_source,
    )
    calls = {"n": 0}

    async def execute(stmt: Any) -> Any:
        calls["n"] += 1
        str(stmt)
        # first execute: select channel
        if calls["n"] == 1:
            return _ReturningResult(channel)
        # insert post ON CONFLICT → None
        return _ReturningResult(None)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.flush = AsyncMock()

    msg = make_message(text="dup")
    result = await upsert_post(session, msg)
    assert result is None
    # only channel select + post insert — no task inserts
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_upsert_new_text_supplier_no_ai_task() -> None:
    channel = SimpleNamespace(
        channel_id=-100123,
        purpose=ParserChannelPurpose.supplier_price_source,
    )
    new_post = SimpleNamespace(
        id=99,
        channel_id=-100123,
        message_id=1,
        content_type=ParserContentType.text,
        error_text=None,
    )
    tables: list[str] = []

    async def execute(stmt: Any) -> Any:
        table = getattr(getattr(stmt, "table", None), "name", None)
        if table:
            tables.append(table)
        if not tables or (len(tables) == 1 and table is None):
            # SELECT channel (no .table on Select in some versions)
            if not hasattr(stmt, "table"):
                return _ReturningResult(channel)
        if table == "parser_posts":
            return _ReturningResult(new_post)
        if table in ("parser_tasks", "parser_media_files"):
            return _ReturningResult(None)
        # first call without table = select channel
        if "parser_channels" in str(stmt).lower() or not tables:
            return _ReturningResult(channel)
        return _ReturningResult(new_post)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.flush = AsyncMock()

    msg = make_message(text="iPhone 70000", media=None)
    result = await upsert_post(session, msg)
    assert result is new_post
    assert "parser_tasks" not in tables
