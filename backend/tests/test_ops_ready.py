"""Liveness, readiness, metrics, and dead-letter replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AdminAuditLog, TelegramOutbox, WebhookInbox
from app.services.dlq_service import ReplayNeedsConfirmation, replay_outbox
from app.services.ops_status_service import sanitize_error
from app.services.heartbeat_service import touch_heartbeat
from app.services.retention_service import run_retention


@pytest.mark.asyncio
async def test_live_ready_and_metrics(client) -> None:
    live = await client.get("/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["schema"] != "mismatch"

    hidden = await client.get("/health/details")
    assert hidden.status_code == 401
    details = await client.get(
        "/health/details",
        headers={"X-Internal-Token": "test-webhook-secret"},
    )
    assert details.status_code == 200
    assert "dead_inbox" in details.json()

    metrics = await client.get("/metrics")
    assert metrics.status_code == 401
    metrics = await client.get(
        "/metrics",
        headers={"X-Metrics-Token": "test-metrics-token"},
    )
    assert metrics.status_code == 200
    assert "zakupki_inbox_pending" in metrics.text


@pytest.mark.asyncio
async def test_ready_requires_worker_when_async(client) -> None:
    settings = get_settings()
    previous = settings.webhook_async_enabled
    settings.webhook_async_enabled = True
    try:
        response = await client.get("/ready")
    finally:
        settings.webhook_async_enabled = previous
    assert response.status_code == 503
    assert "worker" in response.json()["reasons"]


@pytest.mark.asyncio
async def test_ready_requires_scheduler_when_expected(client, db_session) -> None:
    settings = get_settings()
    previous_enabled = settings.scheduler_enabled
    previous_expected = settings.scheduler_expected
    settings.scheduler_enabled = False
    settings.scheduler_expected = True
    try:
        response = await client.get("/ready")
    finally:
        settings.scheduler_enabled = previous_enabled
        settings.scheduler_expected = previous_expected
    assert response.status_code == 503
    assert "scheduler" in response.json()["reasons"]


@pytest.mark.asyncio
async def test_ready_ok_with_fresh_scheduler_heartbeat(client) -> None:
    from app.db.session import get_session_factory

    settings = get_settings()
    previous_enabled = settings.scheduler_enabled
    previous_expected = settings.scheduler_expected
    settings.scheduler_enabled = False
    settings.scheduler_expected = True
    try:
        factory = get_session_factory()
        async with factory() as session:
            await touch_heartbeat(
                session,
                process_type="scheduler",
                instance_id="test-scheduler-ready",
            )
            await session.commit()
        response = await client.get("/ready")
    finally:
        settings.scheduler_enabled = previous_enabled
        settings.scheduler_expected = previous_expected
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sanitize_error_strips_token() -> None:
    token = get_settings().telegram_bot_token
    assert token not in (sanitize_error(f"boom {token}") or "")
    assert "[redacted]" in (sanitize_error(f"boom {token}") or "")


@pytest.mark.asyncio
async def test_uncertain_ask_needs_confirmation(db_session) -> None:
    row = TelegramOutbox(
        dedupe_key="ops-ask",
        chat_id=1,
        kind="ask",
        text="price?",
        status="uncertain",
        attempts=2,
    )
    db_session.add(row)
    await db_session.flush()
    with pytest.raises(ReplayNeedsConfirmation):
        await replay_outbox(db_session, row.id, actor_telegram_id=7, confirmed=False)
    assert await replay_outbox(db_session, row.id, actor_telegram_id=7, confirmed=True) == "replayed"
    await db_session.refresh(row)
    assert row.status == "pending"
    assert row.attempts == 0
    audit = (await db_session.execute(select(AdminAuditLog))).scalars().all()
    assert any(item.action == "dlq_replay" for item in audit)


@pytest.mark.asyncio
async def test_retention_dry_run_keeps_rows_and_live_delete_keeps_pending(db_session) -> None:
    old = datetime.now(UTC) - timedelta(days=90)
    done = WebhookInbox(
        tg_update_id=91001,
        payload={},
        status="done",
        processed_at=old,
        created_at=old,
    )
    pending = WebhookInbox(
        tg_update_id=91002,
        payload={},
        status="pending",
        created_at=old,
    )
    db_session.add_all([done, pending])
    await db_session.flush()

    settings = get_settings()
    previous = settings.retention_dry_run
    settings.retention_dry_run = True
    try:
        report = await run_retention(db_session)
    finally:
        settings.retention_dry_run = previous
    assert report["dry_run"] is True
    assert int(report["inbox_candidates"]) >= 1
    still_there = (
        await db_session.execute(select(WebhookInbox.id).where(WebhookInbox.tg_update_id == 91001))
    ).scalar_one_or_none()
    assert still_there is not None

    settings.retention_dry_run = False
    try:
        await run_retention(db_session)
    finally:
        settings.retention_dry_run = previous
    done_left = (
        await db_session.execute(select(WebhookInbox.id).where(WebhookInbox.tg_update_id == 91001))
    ).scalar_one_or_none()
    pending_left = (
        await db_session.execute(select(WebhookInbox.id).where(WebhookInbox.tg_update_id == 91002))
    ).scalar_one_or_none()
    assert done_left is None
    assert pending_left is not None
