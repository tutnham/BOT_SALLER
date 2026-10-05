"""PostgreSQL queue claim concurrency (SKIP LOCKED)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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
        self.texts: list[str] = []

    async def send_message(self, _chat_id: int, text: str, **_kwargs: object) -> int:
        self.sent += 1
        self.texts.append(text)
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


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=5)


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_reclaim_expired_inbox_lease(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    async def _ok(_session, _payload):
        return "ok"

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _ok)
    db_session.add(
        WebhookInbox(
            tg_update_id=940001,
            payload={"update_id": 940001},
            status=WebhookInboxStatus.processing.value,
            leased_until=_past(),
            lease_owner="dead-worker",
            attempts=1,
        )
    )
    await db_session.flush()

    processed = await process_inbox_batch(db_session, worker_id="reclaim")
    assert processed == 1
    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 940001)
    )
    assert row is not None
    assert row.status == WebhookInboxStatus.done.value
    assert row.lease_owner is None
    assert row.leased_until is None


@pytest.mark.asyncio
async def test_active_lease_not_reclaimed(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookInbox(
            tg_update_id=940002,
            payload={"update_id": 940002},
            status=WebhookInboxStatus.processing.value,
            leased_until=_future(),
            lease_owner="live-worker",
        )
    )
    await db_session.flush()
    rows = await claim_rows(
        db_session,
        WebhookInbox,
        pending_status=WebhookInboxStatus.pending.value,
        processing_status=WebhookInboxStatus.processing.value,
        lease_seconds=30,
        batch_size=10,
        worker_id="other",
    )
    assert rows == []


@pytest.mark.asyncio
async def test_concurrent_claim_same_row_once(engine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            WebhookInbox(
                tg_update_id=940003,
                payload={"update_id": 940003},
                status=WebhookInboxStatus.pending.value,
            )
        )
        await session.commit()

    async def claim_one() -> list[int]:
        async with session_factory() as session:
            rows = await claim_rows(
                session,
                WebhookInbox,
                pending_status=WebhookInboxStatus.pending.value,
                processing_status=WebhookInboxStatus.processing.value,
                lease_seconds=30,
                batch_size=5,
                worker_id="race",
            )
            ids = [int(row.id) for row in rows]
            await session.commit()
            return ids

    first, second = await asyncio.gather(claim_one(), claim_one())
    assert len(set(first) | set(second)) == 1
    assert len(first) + len(second) == 1


@pytest.mark.asyncio
async def test_dead_never_reclaimed(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookInbox(
            tg_update_id=940004,
            payload={"update_id": 940004},
            status=WebhookInboxStatus.dead.value,
            leased_until=_past(),
        )
    )
    await db_session.flush()
    rows = await claim_rows(
        db_session,
        WebhookInbox,
        pending_status=WebhookInboxStatus.pending.value,
        processing_status=WebhookInboxStatus.processing.value,
        lease_seconds=30,
        batch_size=10,
    )
    assert rows == []


@pytest.mark.asyncio
async def test_uncertain_never_reclaimed(db_session: AsyncSession) -> None:
    db_session.add(
        TelegramOutbox(
            dedupe_key="uncertain-940005",
            chat_id=1,
            kind="ask",
            text="x",
            status=TelegramOutboxStatus.uncertain.value,
            leased_until=_past(),
        )
    )
    await db_session.flush()
    rows = await claim_rows(
        db_session,
        TelegramOutbox,
        pending_status=TelegramOutboxStatus.pending.value,
        processing_status=TelegramOutboxStatus.processing.value,
        lease_seconds=30,
        batch_size=10,
    )
    assert rows == []


@pytest.mark.asyncio
async def test_next_attempt_at_respected(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookInbox(
            tg_update_id=940006,
            payload={"update_id": 940006},
            status=WebhookInboxStatus.pending.value,
            next_attempt_at=_future(),
        )
    )
    await db_session.flush()
    rows = await claim_rows(
        db_session,
        WebhookInbox,
        pending_status=WebhookInboxStatus.pending.value,
        processing_status=WebhookInboxStatus.processing.value,
        lease_seconds=30,
        batch_size=10,
    )
    assert rows == []


@pytest.mark.asyncio
async def test_expired_lease_respects_max_attempts(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.telegram import webhook_dispatcher

    calls = 0

    async def _ok(_session, _payload):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _ok)
    monkeypatch.setattr(get_settings(), "worker_max_attempts", 1)
    db_session.add(
        WebhookInbox(
            tg_update_id=940007,
            payload={"update_id": 940007},
            status=WebhookInboxStatus.processing.value,
            leased_until=_past(),
            attempts=0,
        )
    )
    await db_session.flush()
    processed = await process_inbox_batch(db_session, worker_id="maxed")
    assert processed == 0
    assert calls == 0
    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 940007)
    )
    assert row is not None
    assert row.status == WebhookInboxStatus.dead.value
    assert row.last_error == "lease_expired"


@pytest.mark.asyncio
async def test_expired_ask_outbox_becomes_uncertain(db_session: AsyncSession) -> None:
    telegram = _RecordingTelegram()
    db_session.add(
        TelegramOutbox(
            dedupe_key="expired-ask-940008",
            chat_id=1,
            kind="ask",
            text="price?",
            status=TelegramOutboxStatus.processing.value,
            leased_until=_past(),
            lease_owner="crashed",
        )
    )
    await db_session.flush()
    sent = await process_outbox_batch(
        db_session,
        worker_id="reclaim-outbox",
        telegram=telegram,  # type: ignore[arg-type]
    )
    assert sent == 0
    assert all("price?" not in text for text in telegram.texts)
    row = await db_session.scalar(
        select(TelegramOutbox).where(TelegramOutbox.dedupe_key == "expired-ask-940008")
    )
    assert row is not None
    assert row.status == TelegramOutboxStatus.uncertain.value
    assert row.lease_owner is None


@pytest.mark.asyncio
async def test_outbox_not_sent_twice(engine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            TelegramOutbox(
                dedupe_key="once-940009",
                chat_id=1,
                kind="notify",
                text="once",
                status=TelegramOutboxStatus.pending.value,
            )
        )
        await session.commit()
    telegram = _RecordingTelegram()

    async def deliver_once() -> int:
        async with session_factory() as session:
            return await process_outbox_batch(
                session,
                worker_id="send-once",
                telegram=telegram,  # type: ignore[arg-type]
            )

    first, second = await asyncio.gather(deliver_once(), deliver_once())
    assert first + second == 1
    assert telegram.sent == 1


@pytest.mark.asyncio
async def test_duplicate_update_id_single_action(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    calls = 0

    async def _ok(_session, _payload):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _ok)
    db_session.add(
        WebhookInbox(
            tg_update_id=940010,
            payload={"update_id": 940010},
            status=WebhookInboxStatus.pending.value,
        )
    )
    await db_session.flush()
    assert await process_inbox_batch(db_session, worker_id="once") == 1
    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 940010)
    )
    assert row is not None
    row.status = WebhookInboxStatus.pending.value
    row.next_attempt_at = datetime.now(UTC)
    await db_session.flush()
    assert await process_inbox_batch(db_session, worker_id="twice") == 0
    assert calls == 1
    refreshed = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 940010)
    )
    assert refreshed is not None
    assert refreshed.status == WebhookInboxStatus.done.value


@pytest.mark.asyncio
async def test_lease_fields_cleared_after_success_and_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    async def _ok(_session, _payload):
        return "ok"

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _ok)
    db_session.add(
        WebhookInbox(
            tg_update_id=940011,
            payload={"update_id": 940011},
            status=WebhookInboxStatus.pending.value,
        )
    )
    await db_session.flush()
    await process_inbox_batch(db_session, worker_id="lease-clear")
    row = await db_session.scalar(
        select(WebhookInbox).where(WebhookInbox.tg_update_id == 940011)
    )
    assert row is not None
    assert row.lease_owner is None
    assert row.leased_until is None
