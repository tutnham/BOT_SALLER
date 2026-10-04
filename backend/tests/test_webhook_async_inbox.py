"""Async webhook enqueue regression tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import WebhookInbox
from tests.conftest import TELEGRAM_WEBHOOK_SECRET_TOKEN, next_tg_update_id


@pytest.mark.asyncio
async def test_webhook_async_enqueues_without_sync_processing(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("WEBHOOK_ASYNC_ENABLED", "true")
    get_settings.cache_clear()

    update_id = next_tg_update_id()
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1},
            "text": "/start",
        },
    }

    response = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET_TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"

    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert row is not None
    assert row.payload["update_id"] == update_id

    get_settings.cache_clear()
    monkeypatch.delenv("WEBHOOK_ASYNC_ENABLED", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_webhook_async_duplicate_returns_duplicate(
    webhook_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("WEBHOOK_ASYNC_ENABLED", "true")
    get_settings.cache_clear()

    update_id = next_tg_update_id()
    payload = {"update_id": update_id, "message": {"message_id": 2, "chat": {"id": 1}}}

    headers = {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET_TOKEN}
    first = await webhook_client.post("/telegram/webhook", json=payload, headers=headers)
    second = await webhook_client.post("/telegram/webhook", json=payload, headers=headers)

    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "duplicate"

    get_settings.cache_clear()
    monkeypatch.delenv("WEBHOOK_ASYNC_ENABLED", raising=False)
    get_settings.cache_clear()
