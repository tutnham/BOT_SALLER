"""Parser /live, /ready, and protected details."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import get_settings


@pytest.mark.asyncio
async def test_live_without_db(client) -> None:
    response = await client.get("/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_fails_without_runtime_heartbeat(client) -> None:
    settings = get_settings()
    previous = settings.runtime_required
    settings.runtime_required = True
    try:
        response = await client.get("/ready")
    finally:
        settings.runtime_required = previous
    assert response.status_code == 503
    assert "runtime" in response.json()["reasons"]


@pytest.mark.asyncio
async def test_ready_ok_with_fresh_runtime_heartbeat(client) -> None:
    settings = get_settings()
    previous = settings.runtime_required
    settings.runtime_required = True
    try:
        client.runtime_store.append(datetime.now(UTC))
        response = await client.get("/ready")
    finally:
        settings.runtime_required = previous
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_details_requires_token(client) -> None:
    denied = await client.get("/health/details")
    assert denied.status_code == 401
    token = get_settings().api_auth_token
    ok = await client.get("/health/details", headers={"X-Internal-Token": token})
    assert ok.status_code == 200
    assert "tasks_pending" in ok.json()
