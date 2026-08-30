"""API auth: /health open, /posts requires Bearer."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import API_AUTH_TOKEN


@pytest.mark.asyncio
async def test_health_no_auth(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


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
    assert resp.json() == []
