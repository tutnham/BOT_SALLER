"""Worker stop event and SIGTERM lease recovery."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WebhookInbox, WebhookInboxStatus
from app.services.queue_claim import claim_rows
from app.workers.main import _run_loop

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stop_during_tick_does_not_claim_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    started = asyncio.Event()
    ticks = 0

    async def _slow(_factory: object, _worker_id: str) -> None:
        nonlocal ticks
        ticks += 1
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr("app.workers.main._tick", _slow)
    monkeypatch.setattr(get_settings(), "worker_shutdown_grace_seconds", 0.2)
    monkeypatch.setattr(get_settings(), "worker_poll_interval_seconds", 0.05)
    stop = asyncio.Event()

    async def _fire() -> None:
        await started.wait()
        stop.set()

    asyncio.create_task(_fire())
    await _run_loop(None, stop)  # type: ignore[arg-type]
    assert ticks == 1


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM worker test runs on Linux CI")
@pytest.mark.asyncio
async def test_worker_sigterm_leaves_reclaimable_lease(
    engine,
    test_database_url: str,
) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            WebhookInbox(
                tg_update_id=950001,
                payload={"update_id": 950001},
                status=WebhookInboxStatus.pending.value,
            )
        )
        await session.commit()

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": test_database_url,
            "TELEGRAM_BOT_TOKEN": "test-bot-token",
            "WEBHOOK_SECRET": "test-webhook-secret",
            "TELEGRAM_WEBHOOK_SECRET_TOKEN": "test-telegram-webhook-secret",
            "SCHEDULER_ENABLED": "false",
            "WORKER_TEST_PAUSE_SECONDS": "30",
            "WORKER_SHUTDOWN_GRACE_SECONDS": "1",
            "WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "WORKER_ID": "sigterm-test",
        }
    )
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-m", "app.workers.main"],
        cwd=BACKEND_ROOT,
        env=env,
    )
    try:
        await _wait_until_processing(session_factory)
        async with session_factory() as session:
            session.add(
                WebhookInbox(
                    tg_update_id=950002,
                    payload={"update_id": 950002},
                    status=WebhookInboxStatus.pending.value,
                )
            )
            await session.commit()
        proc.send_signal(signal.SIGTERM)
        return_code = await asyncio.to_thread(proc.wait, 20)
    finally:
        if proc.poll() is None:
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)

    assert return_code == 0
    async with session_factory() as session:
        idle = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND application_name = 'zakupki-worker' "
                "AND state = 'idle in transaction'"
            )
        )
        first = await session.scalar(
            select(WebhookInbox).where(WebhookInbox.tg_update_id == 950001)
        )
        second = await session.scalar(
            select(WebhookInbox).where(WebhookInbox.tg_update_id == 950002)
        )
    assert idle == 0
    assert first is not None and first.status == WebhookInboxStatus.processing.value
    assert second is not None and second.status == WebhookInboxStatus.pending.value

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE webhook_inbox SET leased_until = :past "
                "WHERE tg_update_id = 950001"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=1)},
        )
        await session.commit()
        rows = await claim_rows(
            session,
            WebhookInbox,
            pending_status=WebhookInboxStatus.pending.value,
            processing_status=WebhookInboxStatus.processing.value,
            lease_seconds=30,
            batch_size=10,
            worker_id="after-sigterm",
        )
        ids = {int(row.tg_update_id) for row in rows}
        await session.rollback()
    assert 950001 in ids


async def _wait_until_processing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(50):
        async with session_factory() as session:
            row = await session.scalar(
                select(WebhookInbox).where(WebhookInbox.tg_update_id == 950001)
            )
        if row is not None and row.status == WebhookInboxStatus.processing.value:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("worker did not claim the inbox row")
