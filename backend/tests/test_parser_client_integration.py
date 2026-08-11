"""Parser HTTP client — success/failure/timeout, auth header, last_price_sync_at."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import get_settings
from app.db.models import Supplier
from app.services.parser_client import ParserClientError, get_posts
from app.services.price_service import ingest_channel_prices
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_posts_success_and_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8100")
    monkeypatch.setenv("PARSER_API_TOKEN", "secret-token")
    get_settings.cache_clear()

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 11,
                        "message_id": 11,
                        "post_date": "2026-07-30T06:00:00+00:00",
                        "raw_text": "iPhone 15 70000",
                        "message_link": "https://t.me/c/1/11",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        posts = await get_posts(
            channel_id=-1001,
            from_=datetime(2026, 7, 29, tzinfo=UTC),
            http_client=client,
        )

    assert len(posts) == 1
    assert posts[0].post_id == 11
    assert posts[0].raw_text == "iPhone 15 70000"
    assert captured["auth"] == "Bearer secret-token"
    assert "channel_id=-1001" in captured["url"]
    assert "content_type=text" in captured["url"]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8100")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ParserClientError, match="parser_http_500"):
            await get_posts(channel_id=1, from_=None, http_client=client)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8100")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ParserClientError, match="parser_transport_error"):
            await get_posts(channel_id=1, from_=None, http_client=client)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ingest_updates_last_price_sync_at(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
) -> None:
    post_date = datetime(2026, 7, 30, 8, 15, tzinfo=UTC)

    async def _fake_get_posts(**kwargs):
        from app.services.parser_client import ParserPost

        return [
            ParserPost(
                post_id=42,
                message_id=42,
                post_date=post_date,
                raw_text="Samsung S24 256 55000",
                message_link=None,
            )
        ]

    with patch(
        "app.services.price_service.get_posts",
        new=AsyncMock(side_effect=_fake_get_posts),
    ):
        raw_new, degraded = await ingest_channel_prices(db_session)

    assert raw_new == 1
    assert degraded == 0
    await db_session.refresh(seed_channel_supplier)
    assert seed_channel_supplier.last_price_sync_at == post_date


@pytest.mark.asyncio
async def test_ingest_degrades_on_parser_failure(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
) -> None:
    with patch(
        "app.services.price_service.get_posts",
        new=AsyncMock(side_effect=ParserClientError("parser_http_503")),
    ):
        raw_new, degraded = await ingest_channel_prices(db_session)

    assert raw_new == 0
    assert degraded == 1
    await db_session.refresh(seed_channel_supplier)
    assert seed_channel_supplier.last_price_sync_at is None
