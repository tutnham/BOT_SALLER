"""Webhook inbox enqueue and worker processing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import WebhookInbox, WebhookInboxStatus
from app.db.unit_of_work import caller_owns_transaction
from app.services.queue_claim import claim_rows
from app.utils.idempotency import is_duplicate_update, mark_update_processed


async def enqueue_webhook_update(
    session: AsyncSession,
    *,
    tg_update_id: int,
    payload: dict[str, Any],
) -> tuple[str, int | None]:
    """
    Idempotent enqueue. Returns (status, inbox_id).

    status: ``queued`` | ``duplicate``
    """
    if await is_duplicate_update(session, tg_update_id):
        return "duplicate", None

    stmt = (
        insert(WebhookInbox)
        .values(
            tg_update_id=tg_update_id,
            payload=payload,
            status=WebhookInboxStatus.pending.value,
        )
        .on_conflict_do_nothing(index_elements=["tg_update_id"])
        .returning(WebhookInbox.id)
    )
    inbox_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inbox_id is None:
        return "duplicate", None
    return "queued", int(inbox_id)


def _backoff_seconds(attempts: int) -> int:
    return int(min(300, 2 ** min(attempts, 8)))


async def process_inbox_batch(
    session: AsyncSession,
    *,
    worker_id: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Claim inbox rows, commit the lease, then process each row alone."""
    settings = get_settings()
    rows = await claim_rows(
        session,
        WebhookInbox,
        pending_status=WebhookInboxStatus.pending.value,
        processing_status=WebhookInboxStatus.processing.value,
        lease_seconds=settings.worker_lease_seconds,
        batch_size=settings.worker_inbox_batch_size,
        worker_id=worker_id,
    )
    claimed_ids = [int(row.id) for row in rows]
    await session.commit()
    if not claimed_ids:
        return 0

    processed = 0
    for inbox_id in claimed_ids:
        if session_factory is None:
            processed += await _process_inbox_row(session, inbox_id)
            continue
        async with session_factory() as row_session:
            processed += await _process_inbox_row(row_session, inbox_id)
    return processed


async def _process_inbox_row(session: AsyncSession, inbox_id: int) -> int:
    row = await session.get(WebhookInbox, inbox_id)
    if row is None:
        return 0
    token = caller_owns_transaction.set(True)
    try:
        if await is_duplicate_update(session, int(row.tg_update_id)):
            _mark_inbox_done(row)
            await session.commit()
            return 0

        from app.telegram.webhook_dispatcher import process_telegram_update

        await process_telegram_update(session, row.payload)
        await mark_update_processed(session, int(row.tg_update_id))
        _mark_inbox_done(row)
        await session.commit()
        return 1
    except Exception as exc:
        await session.rollback()
        await _record_inbox_failure(session, inbox_id, exc)
        return 0
    finally:
        caller_owns_transaction.reset(token)


def _mark_inbox_done(row: WebhookInbox) -> None:
    row.status = WebhookInboxStatus.done.value
    row.processed_at = datetime.now(UTC)
    row.lease_owner = None
    row.leased_until = None


async def _record_inbox_failure(
    session: AsyncSession,
    inbox_id: int,
    exc: Exception,
) -> None:
    settings = get_settings()
    row = await session.get(WebhookInbox, inbox_id)
    if row is None:
        return
    row.attempts += 1
    row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    row.lease_owner = None
    row.leased_until = None
    if row.attempts >= settings.worker_max_attempts:
        row.status = WebhookInboxStatus.dead.value
        logger.error(
            "webhook_inbox dead id={} update_id={} err={}",
            row.id,
            row.tg_update_id,
            row.last_error,
        )
    else:
        row.status = WebhookInboxStatus.pending.value
        row.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=_backoff_seconds(row.attempts)
        )
        logger.exception(
            "webhook_inbox failed id={} update_id={}",
            row.id,
            row.tg_update_id,
        )
    await session.commit()
