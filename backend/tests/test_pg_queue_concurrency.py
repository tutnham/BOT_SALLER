"""PostgreSQL queue claim concurrency (SKIP LOCKED)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WebhookInbox, WebhookInboxStatus
from app.services.inbox_service import process_inbox_batch
from app.services.queue_claim import claim_rows


@pytest.mark.asyncio
async def test_webhook_inbox_claim_skip_locked(
    engine,
    db_session: AsyncSession,
) -> None:
    for update_id in (910001, 910002, 910003):
        db_session.add(
            WebhookInbox(
                tg_update_id=update_id,
                payload={"update_id": update_id},
                status=WebhookInboxStatus.pending.value,
            )
        )
    await db_session.flush()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def claim_one() -> list[int]:
        async with session_factory() as session:
            rows = await claim_rows(
                session,
                WebhookInbox,
                pending_status=WebhookInboxStatus.pending.value,
                processing_status=WebhookInboxStatus.processing.value,
                lease_seconds=30,
                batch_size=2,
                worker_id="test-worker",
            )
            ids = [int(row.id) for row in rows]
            await session.commit()
            return ids

    claimed_a, claimed_b = await asyncio.gather(claim_one(), claim_one())
    merged = set(claimed_a) | set(claimed_b)
    assert len(merged) == len(claimed_a) + len(claimed_b)
    assert len(merged) <= 3


@pytest.mark.asyncio
async def test_inbox_worker_marks_done(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    async def _noop(_session, _payload):
        return "ok"

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _noop)

    row = WebhookInbox(
        tg_update_id=920001,
        payload={"update_id": 920001},
        status=WebhookInboxStatus.pending.value,
    )
    db_session.add(row)
    await db_session.flush()

    processed = await process_inbox_batch(db_session, worker_id="unit-test")
    assert processed == 1
    refreshed = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 920001)
    )
    assert refreshed is not None
    assert refreshed.status == WebhookInboxStatus.done.value
