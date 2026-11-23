"""POST /channels/backfill: full_history flag -> parser_tasks.history_limit."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db.models import (
    ParserChannel,
    ParserChannelStatus,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from tests.conftest import API_AUTH_TOKEN
from tests.test_channels_api_write import _make_fake_session


def _active_channel() -> ParserChannel:
    return ParserChannel(
        id=1,
        channel_id=-1001234567890,
        username="supplier_prices",
        status=ParserChannelStatus.active,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_backfill_full_history_sets_zero_limit(client: AsyncClient) -> None:
    from app.api.main import app
    from app.db.session import get_db

    session = _make_fake_session()()
    session.channels.append(_active_channel())

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.post(
            "/channels/backfill",
            json={"channel_id": -1001234567890, "full_history": True},
            headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
        )
        assert response.status_code == 202
        assert len(session.tasks) == 1
        assert session.tasks[0].task_type == ParserTaskType.backfill_channel
        assert session.tasks[0].history_limit == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_backfill_default_keeps_limited_history(client: AsyncClient) -> None:
    from app.api.main import app
    from app.db.session import get_db

    session = _make_fake_session()()
    session.channels.append(_active_channel())

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.post(
            "/channels/backfill",
            json={"channel_id": -1001234567890},
            headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
        )
        assert response.status_code == 202
        assert session.tasks[0].history_limit is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_backfill_full_history_upgrades_open_task(client: AsyncClient) -> None:
    from app.api.main import app
    from app.db.session import get_db

    session = _make_fake_session()()
    session.channels.append(_active_channel())
    existing = ParserTask(
        id=7,
        post_id=None,
        channel_id=1,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.new,
        max_attempts=5,
        history_limit=None,
    )
    session.tasks.append(existing)

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.post(
            "/channels/backfill",
            json={"channel_id": -1001234567890, "full_history": True},
            headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
        )
        assert response.status_code == 202
        assert response.json()["task_id"] == 7
        assert len(session.tasks) == 1
        assert existing.history_limit == 0
    finally:
        app.dependency_overrides.clear()