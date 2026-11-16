"""A2: упавшее обновление sync-webhook повторяется через inbox и DLQ."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import UpdateLog, WebhookInbox, WebhookInboxStatus
from app.services.dlq_service import replay_inbox
from app.services.inbox_service import process_inbox_batch
from app.telegram import webhook_dispatcher, webhook_router
from tests.conftest import TELEGRAM_WEBHOOK_SECRET_TOKEN, next_tg_update_id


async def _post_update(webhook_client: AsyncClient, update_id: int) -> dict:
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
    return response.json()


async def _defer_other_inbox_rows(db_session: AsyncSession, keep_update_id: int) -> None:
    """Уводим чужие строки inbox из выборки батча: процесс коммитит lease,
    поэтому строки соседних тестов могут переживать откат savepoint."""
    rows = (
        await db_session.scalars(
            select(WebhookInbox).where(WebhookInbox.tg_update_id != keep_update_id)
        )
    ).all()
    future = datetime.now(UTC) + timedelta(days=1)
    for row in rows:
        if row.status in (
            WebhookInboxStatus.pending.value,
            WebhookInboxStatus.processing.value,
        ):
            row.next_attempt_at = future
            row.leased_until = future
    await db_session.flush()


@pytest.mark.asyncio
async def test_sync_failure_enqueues_inbox_and_clears_update_log(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(_session, _payload):
        raise RuntimeError("sync route exploded")

    monkeypatch.setattr(webhook_router, "_route_update", _fail)
    update_id = next_tg_update_id()

    body = await _post_update(webhook_client, update_id)
    assert body["status"] == "error"

    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert row is not None
    assert row.status == WebhookInboxStatus.pending.value
    assert row.attempts == 1
    assert row.last_error is not None
    assert "sync route exploded" in row.last_error
    logged = await db_session.scalar(
        select(UpdateLog).where(UpdateLog.tg_update_id == update_id)
    )
    assert logged is None


@pytest.mark.asyncio
async def test_sync_failure_retry_succeeds_via_inbox(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(_session, _payload):
        raise RuntimeError("first attempt fails")

    monkeypatch.setattr(webhook_router, "_route_update", _fail)
    update_id = next_tg_update_id()
    body = await _post_update(webhook_client, update_id)
    assert body["status"] == "error"

    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert row is not None
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    await _defer_other_inbox_rows(db_session, update_id)

    async def _ok(_session, _payload):
        return "ok"

    monkeypatch.setattr(webhook_router, "_route_update", _ok)
    # диспетчер inbox держит собственную ссылку на _route_update (import by value)
    monkeypatch.setattr(webhook_dispatcher, "_route_update", _ok)
    processed = await process_inbox_batch(db_session, worker_id="sync-retry")
    assert processed == 1

    refreshed = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert refreshed is not None
    assert refreshed.status == WebhookInboxStatus.done.value
    logged = await db_session.scalar(
        select(UpdateLog).where(UpdateLog.tg_update_id == update_id)
    )
    assert logged is not None


@pytest.mark.asyncio
async def test_sync_failure_dead_after_max_attempts_and_replay(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(_session, _payload):
        raise RuntimeError("persistent failure")

    monkeypatch.setattr(webhook_router, "_route_update", _fail)
    update_id = next_tg_update_id()
    body = await _post_update(webhook_client, update_id)
    assert body["status"] == "error"

    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert row is not None
    row.attempts = get_settings().worker_max_attempts - 1
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    await _defer_other_inbox_rows(db_session, update_id)

    # повтор через inbox тоже должен падать: патчим ссылку диспетчера
    monkeypatch.setattr(webhook_dispatcher, "_route_update", _fail)
    processed = await process_inbox_batch(db_session, worker_id="sync-dlq")
    assert processed == 0
    refreshed = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert refreshed is not None
    assert refreshed.status == WebhookInboxStatus.dead.value

    result = await replay_inbox(db_session, refreshed.id, actor_telegram_id=None)
    assert result == "replayed"
    replayed = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == update_id)
    )
    assert replayed is not None
    assert replayed.status == WebhookInboxStatus.pending.value
    assert replayed.attempts == 0