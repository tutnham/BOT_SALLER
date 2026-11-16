"""B1: обработка редактирований и удалений постов канала."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.db.models import ParserChannelPurpose, ParserContentType
from app.mtproto.upsert import apply_post_edit, mark_posts_deleted
from tests.conftest import API_AUTH_TOKEN, make_message


class _ReturningResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value


def _channel() -> SimpleNamespace:
    return SimpleNamespace(
        channel_id=-100123,
        purpose=ParserChannelPurpose.supplier_price_source,
    )


def _stored_post(**overrides: Any) -> SimpleNamespace:
    base = {
        "id": 50,
        "channel_id": -100123,
        "message_id": 1,
        "grouped_id": None,
        "post_date": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "content_type": ParserContentType.text,
        "raw_text": "iPhone 15 70000",
        "message_link": "https://t.me/c/123/1",
        "raw_metadata": {"text": "iPhone 15 70000"},
        "edited_at": None,
        "excluded": False,
        "excluded_reason": None,
        "excluded_at": None,
        "error_text": None,
        "status": "new",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_apply_edit_updates_text_and_keeps_post_date() -> None:
    post = _stored_post()
    original_date = post.post_date
    edit_date = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    results = iter([_channel(), post])

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda stmt: _ReturningResult(next(results)))
    session.flush = AsyncMock()

    msg = make_message(text="iPhone 15 65000", edit_date=edit_date)
    result = await apply_post_edit(session, msg)

    assert result is post
    assert post.raw_text == "iPhone 15 65000"
    assert post.raw_metadata["text"] == "iPhone 15 65000"
    assert post.edited_at == edit_date
    # post_date не меняется — курсорная пагинация остаётся стабильной
    assert post.post_date == original_date


@pytest.mark.asyncio
async def test_repeat_edit_same_date_is_noop() -> None:
    edit_date = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    post = _stored_post(raw_text="iPhone 15 65000", edited_at=edit_date)
    results = iter([_channel(), post])

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda stmt: _ReturningResult(next(results)))
    session.flush = AsyncMock()

    # Повторная доставка того же edit_date не должна перезаписывать данные
    msg = make_message(text="подменённый текст", edit_date=edit_date)
    result = await apply_post_edit(session, msg)

    assert result is post
    assert post.raw_text == "iPhone 15 65000"
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_naive_edit_date_normalized_to_utc() -> None:
    post = _stored_post()
    results = iter([_channel(), post])

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda stmt: _ReturningResult(next(results)))
    session.flush = AsyncMock()

    # Pyrogram может отдавать naive datetime (UTC)
    msg = make_message(text="new", edit_date=datetime(2026, 1, 1, 15, 30))
    await apply_post_edit(session, msg)

    assert post.edited_at == datetime(2026, 1, 1, 15, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_edit_missing_post_falls_back_to_insert() -> None:
    new_post = _stored_post(id=51)
    # 1: select channel, 2: select post (None), 3: select channel (upsert), 4: insert
    results = iter([_channel(), None, _channel(), new_post])

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda stmt: _ReturningResult(next(results)))
    session.flush = AsyncMock()

    msg = make_message(text="iPhone 15 65000", edit_date=datetime(2026, 1, 1, 16, 0, tzinfo=UTC))
    result = await apply_post_edit(session, msg)

    assert result is new_post
    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_edit_unregistered_channel_returns_none() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ReturningResult(None))

    result = await apply_post_edit(session, make_message(text="x"))
    assert result is None


@pytest.mark.asyncio
async def test_mark_deleted_sets_excluded_with_reason() -> None:
    captured: list[Any] = []

    class _RowcountResult:
        rowcount = 2

    async def execute(stmt: Any) -> Any:
        captured.append(stmt)
        return _RowcountResult()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.flush = AsyncMock()

    count = await mark_posts_deleted(session, channel_id=-100123, message_ids=[1, 2])

    assert count == 2
    stmt = captured[0]
    assert stmt.table.name == "parser_posts"
    params = stmt.compile().params
    assert params["excluded"] is True
    assert params["excluded_reason"] == "deleted_in_channel"
    assert params["excluded_at"] is not None


@pytest.mark.asyncio
async def test_mark_deleted_empty_ids_is_noop() -> None:
    session = AsyncMock()
    count = await mark_posts_deleted(session, channel_id=-100123, message_ids=[])
    assert count == 0
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_excluded_post_not_returned_by_posts_api(client: AsyncClient) -> None:
    client.posts_store.clear()  # type: ignore[attr-defined]
    client.posts_store.append(  # type: ignore[attr-defined]
        _stored_post(id=1, message_id=10, raw_text="актуальный прайс")
    )
    client.posts_store.append(  # type: ignore[attr-defined]
        _stored_post(
            id=2,
            message_id=11,
            raw_text="удалённый прайс",
            excluded=True,
            excluded_reason="deleted_in_channel",
            excluded_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )

    resp = await client.get(
        "/posts",
        params={"channel_id": -100123, "content_type": "text"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [1]