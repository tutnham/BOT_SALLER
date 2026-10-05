"""PostgreSQL queue claim concurrency (SKIP LOCKED)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    TelegramOutbox,
    TelegramOutboxStatus,
    UpdateLog,
    WebhookInbox,
    WebhookInboxStatus,
)
from app.services.inbox_service import process_inbox_batch
from app.services.outbox_service import process_outbox_batch
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


class _RecordingTelegram:
    def __init__(self) -> None:
        self.sent = 0

    async def send_message(self, *_args: object, **_kwargs: object) -> int:
        self.sent += 1
        return 501


@pytest.mark.asyncio
async def test_inbox_failure_before_commit_keeps_update(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    async def _fail(_session, _payload):
        raise RuntimeError("before commit")

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _fail)
    db_session.add(
        WebhookInbox(
            tg_update_id=930001,
            payload={"update_id": 930001},
            status=WebhookInboxStatus.pending.value,
        )
    )
    await db_session.flush()

    processed = await process_inbox_batch(db_session, worker_id="fail-test")
    assert processed == 0
    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 930001)
    )
    assert row is not None
    assert row.status == WebhookInboxStatus.pending.value
    assert row.attempts == 1
    assert row.lease_owner is None
    logged = await db_session.scalar(
        select(UpdateLog).where(UpdateLog.tg_update_id == 930001)
    )
    assert logged is None


@pytest.mark.asyncio
async def test_handler_partial_commit_does_not_mark_update_done(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    async def _partial(session, _payload):
        await session.commit()
        raise RuntimeError("after partial commit")

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _partial)
    db_session.add(
        WebhookInbox(
            tg_update_id=930002,
            payload={"update_id": 930002},
            status=WebhookInboxStatus.pending.value,
        )
    )
    await db_session.flush()

    await process_inbox_batch(db_session, worker_id="partial-test")
    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 930002)
    )
    assert row is not None
    assert row.status == WebhookInboxStatus.pending.value
    logged = await db_session.scalar(
        select(UpdateLog).where(UpdateLog.tg_update_id == 930002)
    )
    assert logged is None


@pytest.mark.asyncio
async def test_outbox_crash_after_send_not_rolled_back_to_pending(
    db_session: AsyncSession,
) -> None:
    telegram = _RecordingTelegram()
    db_session.add(
        TelegramOutbox(
            dedupe_key="crash-after-send-930003",
            chat_id=1,
            kind="notify",
            text="hello",
            status=TelegramOutboxStatus.pending.value,
        )
    )
    await db_session.flush()

    async def _crash() -> None:
        raise RuntimeError("killed after send")

    sent = await process_outbox_batch(
        db_session,
        worker_id="crash-test",
        telegram=telegram,  # type: ignore[arg-type]
        on_after_send=_crash,
    )
    assert sent == 0
    assert telegram.sent == 1
    row = await db_session.scalar(
        select(TelegramOutbox).where(TelegramOutbox.dedupe_key == "crash-after-send-930003")
    )
    assert row is not None
    assert row.status == TelegramOutboxStatus.processing.value

    again = await process_outbox_batch(
        db_session,
        worker_id="crash-test-2",
        telegram=telegram,  # type: ignore[arg-type]
    )
    assert again == 0
    assert telegram.sent == 1
