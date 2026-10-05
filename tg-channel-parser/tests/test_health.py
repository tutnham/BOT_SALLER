"""Parser API health endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_reports_db_ok(client: AsyncClient) -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def connect_cm():
        yield conn

    engine = MagicMock()
    engine.connect = MagicMock(side_effect=lambda: connect_cm())

    with patch("app.api.main.get_engine", return_value=engine):
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


@pytest.mark.asyncio
async def test_health_reports_db_error(client: AsyncClient) -> None:
    with patch("app.api.main.get_engine", side_effect=RuntimeError("db down")):
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "error"}
