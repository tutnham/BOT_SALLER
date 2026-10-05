"""Prometheus exposition: process-local counters + DB-backed gauges."""

from __future__ import annotations

from datetime import UTC, datetime

from prometheus_client import generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun, TelegramOutbox, WebhookInbox
from app.services.heartbeat_service import latest_heartbeat_at
from app.services.telemetry import get_metrics, get_registry


async def _count_webhook_inbox(session: AsyncSession, status: str, *, open_dead: bool = False) -> int:
    stmt = select(func.count()).select_from(WebhookInbox).where(WebhookInbox.status == status)
    if open_dead:
        stmt = stmt.where(WebhookInbox.resolved_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())


async def _count_outbox(session: AsyncSession, status: str, *, open_only: bool = False) -> int:
    stmt = select(func.count()).select_from(TelegramOutbox).where(TelegramOutbox.status == status)
    if open_only:
        stmt = stmt.where(TelegramOutbox.resolved_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())


async def render_metrics(session: AsyncSession) -> str:
    metrics = get_metrics()
    now = datetime.now(UTC)

    metrics.inbox_pending.set(await _count_webhook_inbox(session, "pending"))
    metrics.inbox_processing.set(await _count_webhook_inbox(session, "processing"))
    metrics.inbox_dead.set(await _count_webhook_inbox(session, "dead", open_dead=True))
    metrics.outbox_pending.set(await _count_outbox(session, "pending"))
    metrics.outbox_processing.set(await _count_outbox(session, "processing"))
    metrics.outbox_uncertain.set(await _count_outbox(session, "uncertain", open_only=True))

    oldest = (
        await session.execute(
            select(func.min(WebhookInbox.created_at)).where(WebhookInbox.status == "pending")
        )
    ).scalar_one_or_none()
    oldest_age = 0
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_age = int((now - oldest).total_seconds())
    metrics.inbox_oldest_pending_seconds.set(oldest_age)

    worker_at = await latest_heartbeat_at(session, "worker")
    scheduler_at = await latest_heartbeat_at(session, "scheduler")

    def _age(value: datetime | None) -> float:
        if value is None:
            return -1
        return float(int((now - value).total_seconds()))

    metrics.worker_heartbeat_age_seconds.set(_age(worker_at))
    metrics.scheduler_heartbeat_age_seconds.set(_age(scheduler_at))

    last_morning = (
        await session.execute(
            select(func.max(JobRun.finished_at)).where(
                JobRun.job_name == "morning-price",
                JobRun.status == "ok",
            )
        )
    ).scalar_one_or_none()
    morning_unix = 0.0
    if last_morning is not None:
        if last_morning.tzinfo is None:
            last_morning = last_morning.replace(tzinfo=UTC)
        morning_unix = last_morning.timestamp()
    metrics.morning_price_last_success_unixtime.set(morning_unix)

    return generate_latest(get_registry()).decode("utf-8")
