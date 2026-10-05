"""Prometheus text exposition. Labels stay bounded: no ids, secrets, or message text."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelegramOutbox, WebhookInbox
from app.services.heartbeat_service import latest_heartbeat_at

_webhook_total = 0
_webhook_errors = 0


def record_webhook(*, error: bool) -> None:
    global _webhook_total, _webhook_errors
    _webhook_total += 1
    if error:
        _webhook_errors += 1


def _line(name: str, value: int | float) -> str:
    return f"{name} {value}"


async def render_metrics(session: AsyncSession) -> str:
    now = datetime.now(UTC)

    async def _count(model: type, status: str) -> int:
        column = model.status  # type: ignore[attr-defined]
        return int(
            (await session.execute(select(func.count()).select_from(model).where(column == status))).scalar_one()
        )

    pending = await _count(WebhookInbox, "pending")
    dead = await _count(WebhookInbox, "dead")
    out_pending = await _count(TelegramOutbox, "pending")
    uncertain = await _count(TelegramOutbox, "uncertain")
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
    worker_at = await latest_heartbeat_at(session, "worker")
    scheduler_at = await latest_heartbeat_at(session, "scheduler")

    def _age(value: datetime | None) -> int:
        if value is None:
            return -1
        return int((now - value).total_seconds())

    lines = [
        "# HELP zakupki_webhook_total Webhook requests handled by this process",
        "# TYPE zakupki_webhook_total counter",
        _line("zakupki_webhook_total", _webhook_total),
        "# TYPE zakupki_webhook_errors counter",
        _line("zakupki_webhook_errors", _webhook_errors),
        "# TYPE zakupki_inbox_pending gauge",
        _line("zakupki_inbox_pending", pending),
        "# TYPE zakupki_inbox_dead gauge",
        _line("zakupki_inbox_dead", dead),
        "# TYPE zakupki_outbox_pending gauge",
        _line("zakupki_outbox_pending", out_pending),
        "# TYPE zakupki_outbox_uncertain gauge",
        _line("zakupki_outbox_uncertain", uncertain),
        "# TYPE zakupki_inbox_oldest_pending_seconds gauge",
        _line("zakupki_inbox_oldest_pending_seconds", oldest_age),
        "# TYPE zakupki_worker_heartbeat_age_seconds gauge",
        _line("zakupki_worker_heartbeat_age_seconds", _age(worker_at)),
        "# TYPE zakupki_scheduler_heartbeat_age_seconds gauge",
        _line("zakupki_scheduler_heartbeat_age_seconds", _age(scheduler_at)),
    ]
    return "\n".join(lines) + "\n"
