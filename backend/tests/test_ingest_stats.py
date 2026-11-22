"""Счётчики ingest_channel_prices: чем джоб объясняет пустой прайс."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Supplier
from app.services.parser_client import ParserClientError, ParserPost
from app.services.price_service import IngestStats, ingest_channel_prices

POST_DATE = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


def _posts(monkeypatch: pytest.MonkeyPatch, content_type: str, posts: list[ParserPost]) -> None:
    async def _fake(**kwargs):
        if kwargs.get("content_type") != content_type:
            return
        yield posts

    monkeypatch.setattr("app.services.price_service.iter_posts", _fake)


@pytest.mark.asyncio
async def test_stats_count_posts_without_text(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _posts(
        monkeypatch,
        "text",
        [
            ParserPost(post_id=1, message_id=1, post_date=POST_DATE, raw_text=None),
            ParserPost(post_id=2, message_id=2, post_date=POST_DATE, raw_text="   "),
            ParserPost(post_id=3, message_id=3, post_date=POST_DATE, raw_text="iPhone 16 256 80000"),
        ],
    )

    stats = await ingest_channel_prices(db_session)

    assert (stats.raw_new, stats.degraded) == (1, 0)
    assert stats.channels_checked == 1
    assert stats.posts_seen == 3
    assert stats.posts_without_text == 2
    assert stats.faults == ()


@pytest.mark.asyncio
async def test_stats_are_backward_compatible_pair(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _posts(
        monkeypatch,
        "text",
        [ParserPost(post_id=11, message_id=11, post_date=POST_DATE, raw_text="iPhone 17 Pro 256 90000")],
    )

    stats = await ingest_channel_prices(db_session)
    raw_new, degraded, *_rest = stats

    assert isinstance(stats, IngestStats)
    assert (raw_new, degraded) == (1, 0)


@pytest.mark.asyncio
async def test_stats_record_parser_fault(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(**_kwargs):
        raise ParserClientError("parser_http_503")
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.price_service.iter_posts", _fail)

    stats = await ingest_channel_prices(db_session)

    assert stats.channels_checked == 1
    assert stats.posts_seen == 0
    assert stats.faults == ("http_error",)


@pytest.mark.asyncio
async def test_stats_zero_channels_when_none_bound(
    db_session: AsyncSession,
) -> None:
    stats = await ingest_channel_prices(db_session)

    assert stats == IngestStats(raw_new=0, degraded=0)
