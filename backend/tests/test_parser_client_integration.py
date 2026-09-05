"""Parser HTTP client — success/failure/timeout, auth header, pagination, real arrays."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import get_settings
from app.db.models import Supplier
from app.services.parser_client import ParserClientError, get_posts, list_channels
from app.services.price_service import ingest_channel_prices
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_posts_success_with_raw_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
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
                ],
                "next_cursor": None,
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
async def test_get_posts_pagination_follows_next_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    get_settings.cache_clear()

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "cursor=" not in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 1,
                            "message_id": 1,
                            "post_date": "2026-07-30T06:00:00+00:00",
                            "raw_text": "first",
                        }
                    ],
                    "next_cursor": "page-2",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 2,
                        "message_id": 2,
                        "post_date": "2026-07-30T07:00:00+00:00",
                        "raw_text": "second",
                    }
                ],
                "next_cursor": None,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        posts = await get_posts(channel_id=-1001, from_=None, http_client=client)

    assert len(posts) == 2
    assert [post.raw_text for post in posts] == ["first", "second"]
    assert len(calls) == 2
    assert "cursor=page-2" in calls[1]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_legacy_list_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "message_id": 7,
                    "post_date": "2026-07-30T06:00:00+00:00",
                    "raw_text": "legacy list",
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        posts = await get_posts(channel_id=-1001, from_=None, http_client=client)

    assert len(posts) == 1
    assert posts[0].raw_text == "legacy list"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_list_channels_accepts_raw_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "channel_id": -1001,
                    "username": "supplier_prices",
                    "title": "Supplier",
                    "purpose": "supplier_price_source",
                    "status": "active",
                    "is_active": True,
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        channels = await list_channels(http_client=client)

    assert len(channels) == 1
    assert channels[0].username == "supplier_prices"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ParserClientError, match="parser_invalid_json"):
            await get_posts(channel_id=1, from_=None, http_client=client)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ParserClientError, match="parser_http_500"):
            await get_posts(channel_id=1, from_=None, http_client=client)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ParserClientError, match="parser_http_401"):
            await get_posts(channel_id=1, from_=None, http_client=client)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_posts_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
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
