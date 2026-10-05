"""Readiness and owner-facing operational snapshot. No secrets in the payload."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import JobRun, TelegramOutbox, WebhookInbox
from app.db.revision import EXPECTED_ALEMBIC_REVISION
from app.services.heartbeat_service import heartbeat_is_fresh, latest_heartbeat_at


def details_token_ok(presented: str | None) -> bool:
    settings = get_settings()
    expected = settings.health_details_token or settings.webhook_secret
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def sanitize_error(value: str | None, *, limit: int = 200) -> str | None:
    if not value:
        return None
    settings = get_settings()
    cleaned = " ".join(value.split())
    for secret in (
        settings.telegram_bot_token,
        settings.webhook_secret,
        settings.telegram_webhook_secret_token,
        settings.parser_api_token,
        settings.health_details_token,
        settings.llm_api_key,
    ):
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:limit]


async def _alembic_revision(session: AsyncSession) -> str | None:
    try:
        return (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:
        return None


async def readiness(session: AsyncSession) -> tuple[bool, dict[str, object]]:
    settings = get_settings()
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    revision = await _alembic_revision(session) if db_ok else None
    schema_ok = revision == EXPECTED_ALEMBIC_REVISION
    reasons: list[str] = []
    if not db_ok:
        reasons.append("database")
    if not schema_ok:
        reasons.append("schema")
    worker_ok = True
    if settings.webhook_async_enabled:
        worker_at = await latest_heartbeat_at(session, "worker") if db_ok else None
        worker_ok = heartbeat_is_fresh(worker_at)
        if not worker_ok:
            reasons.append("worker")
    scheduler_ok = True
    if settings.scheduler_enabled or settings.scheduler_expected:
        scheduler_at = await latest_heartbeat_at(session, "scheduler") if db_ok else None
        scheduler_ok = heartbeat_is_fresh(scheduler_at)
        if not scheduler_ok:
            reasons.append("scheduler")
    ready = db_ok and schema_ok and worker_ok and scheduler_ok
    return ready, {
        "status": "ok" if ready else "not_ready",
        "db": "ok" if db_ok else "error",
        "schema": revision if schema_ok else "mismatch",
        "reasons": reasons,
    }


async def health_details(session: AsyncSession) -> dict[str, object]:
    ready, ready_body = await readiness(session)
    now = datetime.now(UTC)
    dead_inbox = int(
        (
            await session.execute(
                select(func.count())
                .select_from(WebhookInbox)
                .where(WebhookInbox.status == "dead", WebhookInbox.resolved_at.is_(None))
            )
        ).scalar_one()
    )
    uncertain = int(
        (
            await session.execute(
                select(func.count())
                .select_from(TelegramOutbox)
                .where(
                    TelegramOutbox.status == "uncertain",
                    TelegramOutbox.resolved_at.is_(None),
                )
            )
        ).scalar_one()
    )
    oldest_pending = (
        await session.execute(
            select(func.min(WebhookInbox.created_at)).where(WebhookInbox.status == "pending")
        )
    ).scalar_one_or_none()
    oldest_age = None
    if oldest_pending is not None:
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=UTC)
        oldest_age = int((now - oldest_pending).total_seconds())
    runs = (
        await session.execute(select(JobRun).order_by(JobRun.finished_at.desc()).limit(10))
    ).scalars().all()
    return {
        "ready": ready,
        "readiness": ready_body,
        "dead_inbox": dead_inbox,
        "uncertain_outbox": uncertain,
        "oldest_pending_age_seconds": oldest_age,
        "recent_jobs": [
            {
                "job_name": row.job_name,
                "status": row.status,
                "finished_at": row.finished_at.isoformat(),
                "error_type": row.error_type,
            }
            for row in runs
        ],
        "commit_sha": get_settings().commit_sha,
    }
