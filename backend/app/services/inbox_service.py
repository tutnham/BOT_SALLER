"""Webhook inbox enqueue and worker processing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import WebhookInbox, WebhookInboxStatus
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
) -> int:
    """Claim and process webhook inbox rows. Returns processed count."""
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
    if not rows:
        return 0

    processed = 0
    for row in rows:
        try:
            if await is_duplicate_update(session, int(row.tg_update_id)):
                row.status = WebhookInboxStatus.done.value
                row.processed_at = datetime.now(UTC)
                continue

            duplicate = await mark_update_processed(session, int(row.tg_update_id))
            if duplicate:
                row.status = WebhookInboxStatus.done.value
                row.processed_at = datetime.now(UTC)
                continue

            from app.telegram.webhook_dispatcher import process_telegram_update

            await process_telegram_update(session, row.payload)
            row.status = WebhookInboxStatus.done.value
            row.processed_at = datetime.now(UTC)
            processed += 1
        except Exception as exc:
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
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
        finally:
            row.lease_owner = None
            row.leased_until = None
            await session.flush()

    return processed
