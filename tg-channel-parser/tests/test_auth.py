"""API auth: /health open, /posts requires Bearer."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from tests.conftest import API_AUTH_TOKEN


@pytest.mark.asyncio
async def test_health_no_auth(client: AsyncClient) -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def connect_cm():
        yield conn

    engine = MagicMock()
    engine.connect = MagicMock(side_effect=lambda: connect_cm())

    with patch("app.api.main.get_engine", return_value=engine):
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


@pytest.mark.asyncio
async def test_posts_missing_token(client: AsyncClient) -> None:
    resp = await client.get("/posts", params={"channel_id": 1})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_posts_bad_token(client: AsyncClient) -> None:
    resp = await client.get(
        "/posts",
        params={"channel_id": 1},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_posts_ok_token(client: AsyncClient) -> None:
    resp = await client.get(
        "/posts",
        params={"channel_id": 1, "content_type": "text"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}
