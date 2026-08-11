"""GET /health — no auth, DB status without 500 on DB failure."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


@pytest.mark.asyncio
async def test_health_db_error_does_not_return_500() -> None:
    """When DB is unreachable, endpoint still returns 200 with db=error."""

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(side_effect=RuntimeError("connection refused"))
    mock_engine.connect.return_value = mock_conn

    with patch("app.main.get_engine", return_value=mock_engine):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "error"
