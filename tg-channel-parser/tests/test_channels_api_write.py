"""Tests for POST /channels and DELETE /channels/{id}."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

from app.db.models import ParserChannel, ParserChannelStatus, ParserTask, ParserTaskType
from tests.conftest import API_AUTH_TOKEN


def _make_fake_session():
    class _Result:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def scalars(self) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: list(self._rows))

        def scalar_one_or_none(self) -> Any:
            return self._rows[0] if self._rows else None

        def scalar_one(self) -> Any:
            return self._rows[0]

    class FakeSession:
        def __init__(self) -> None:
            self.channels: list[ParserChannel] = []
            self.tasks: list[ParserTask] = []
            self._next_id = 1

        async def execute(self, stmt: Any) -> _Result:
            sql = str(stmt).lower()
            if "parser_channels" in sql or "parserchannel" in sql:
                return _Result(self.channels)
            return _Result([])

        async def flush(self) -> None:
            for ch in self.channels:
                if ch.id is None:
                    ch.id = self._next_id
                    self._next_id += 1
            for task in self.tasks:
                if task.id is None:
                    task.id = self._next_id
                    self._next_id += 1

        async def commit(self) -> None:
            return None

        async def get(self, model, ident):
            for ch in self.channels:
                if ch.id == ident:
                    return ch
            return None

        async def close(self) -> None:
            return None

        def add(self, obj: Any) -> None:
            if isinstance(obj, ParserChannel):
                self.channels.append(obj)
            elif isinstance(obj, ParserTask):
                self.tasks.append(obj)

        async def delete(self, obj: Any) -> None:
            if isinstance(obj, ParserChannel) and obj in self.channels:
                self.channels.remove(obj)

    return FakeSession


@pytest.fixture
def fake_session():
    return _make_fake_session()


@pytest.mark.asyncio
async def test_create_channel_pending_and_task(client: AsyncClient, fake_session: Any) -> None:
    from app.api.main import app
    from app.db.session import get_db

    session = fake_session()

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.post(
            "/channels",
            json={"handle": "@supplier_prices"},
            headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
        )
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "pending"
        assert data["username"] == "supplier_prices"

        assert len(session.channels) == 1
        assert session.channels[0].status == ParserChannelStatus.pending
        assert len(session.tasks) == 1
        assert session.tasks[0].task_type == ParserTaskType.resolve_channel
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_channel_deactivates(client: AsyncClient, fake_session: Any) -> None:
    from app.api.main import app
    from app.db.session import get_db

    session = fake_session()
    session.channels.append(
        ParserChannel(
            id=1,
            channel_id=-1001234567890,
            username="supplier_prices",
            status=ParserChannelStatus.active,
            is_active=True,
        )
    )

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.delete(
            "/channels/1",
            headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
        )
        assert response.status_code == 200
        assert session.channels[0].is_active is False
        assert session.channels[0].status == ParserChannelStatus.failed
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_channel_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/channels", json={"handle": "@supplier_prices"})
    assert response.status_code == 401
