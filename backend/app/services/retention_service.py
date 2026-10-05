"""Delete only terminal queue rows and expired parse cache. Dry-run by default."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ParseCache, TelegramOutbox, WebhookInbox
from app.services.audit_service import record_audit


async def _count_inbox(session: AsyncSession, cutoff: datetime) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(WebhookInbox)
                .where(
                    WebhookInbox.status == "done",
                    WebhookInbox.processed_at.is_not(None),
                    WebhookInbox.processed_at < cutoff,
                )
            )
        ).scalar_one()
    )


async def _count_outbox(session: AsyncSession, cutoff: datetime) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(TelegramOutbox)
                .where(
                    TelegramOutbox.status == "sent",
                    TelegramOutbox.sent_at.is_not(None),
                    TelegramOutbox.sent_at < cutoff,
                )
            )
        ).scalar_one()
    )


async def run_retention(session: AsyncSession) -> dict[str, int | bool]:
    settings = get_settings()
    now = datetime.now(UTC)
    inbox_cutoff = now - timedelta(days=settings.retention_inbox_days)
    outbox_cutoff = now - timedelta(days=settings.retention_outbox_days)
    cache_cutoff = now - timedelta(days=settings.retention_parse_cache_days)
    inbox_count = await _count_inbox(session, inbox_cutoff)
    outbox_count = await _count_outbox(session, outbox_cutoff)
    cache_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ParseCache)
                .where(ParseCache.created_at < cache_cutoff)
            )
        ).scalar_one()
    )
    deleted_inbox = 0
    deleted_outbox = 0
    deleted_cache = 0
    if not settings.retention_dry_run:
        batch = settings.retention_batch_size
        inbox_ids = (
            await session.execute(
                select(WebhookInbox.id)
                .where(
                    WebhookInbox.status == "done",
                    WebhookInbox.processed_at.is_not(None),
                    WebhookInbox.processed_at < inbox_cutoff,
                )
                .limit(batch)
            )
        ).scalars().all()
        if inbox_ids:
            result = await session.execute(delete(WebhookInbox).where(WebhookInbox.id.in_(inbox_ids)))
            deleted_inbox = int(result.rowcount or 0)
        outbox_ids = (
            await session.execute(
                select(TelegramOutbox.id)
                .where(
                    TelegramOutbox.status == "sent",
                    TelegramOutbox.sent_at.is_not(None),
                    TelegramOutbox.sent_at < outbox_cutoff,
                )
                .limit(batch)
            )
        ).scalars().all()
        if outbox_ids:
            result = await session.execute(
                delete(TelegramOutbox).where(TelegramOutbox.id.in_(outbox_ids))
            )
            deleted_outbox = int(result.rowcount or 0)
        cache_ids = (
            await session.execute(
                select(ParseCache.id).where(ParseCache.created_at < cache_cutoff).limit(batch)
            )
        ).scalars().all()
        if cache_ids:
            result = await session.execute(delete(ParseCache).where(ParseCache.id.in_(cache_ids)))
            deleted_cache = int(result.rowcount or 0)
    await record_audit(
        session,
        action="retention_run",
        entity_type="retention",
        entity_id="queue_and_parse_cache",
        new_state={
            "dry_run": settings.retention_dry_run,
            "inbox_candidates": inbox_count,
            "outbox_candidates": outbox_count,
            "cache_candidates": cache_count,
            "deleted_inbox": deleted_inbox,
            "deleted_outbox": deleted_outbox,
            "deleted_cache": deleted_cache,
        },
    )
    return {
        "dry_run": settings.retention_dry_run,
        "inbox_candidates": inbox_count,
        "outbox_candidates": outbox_count,
        "deleted_inbox": deleted_inbox,
        "deleted_outbox": deleted_outbox,
        "deleted_cache": deleted_cache,
    }
