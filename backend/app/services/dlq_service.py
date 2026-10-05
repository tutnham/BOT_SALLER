"""Operator replay and resolve for terminal queue rows. Never auto-retries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelegramOutbox, WebhookInbox
from app.services.audit_service import record_audit
from app.services.ops_status_service import sanitize_error

_UNSAFE_KINDS = frozenset({"ask", "bargain"})


class ReplayNeedsConfirmation(Exception):
    """Uncertain ask/bargain must not be resent until the operator confirms."""


def _public_row(kind: str, row: WebhookInbox | TelegramOutbox) -> dict[str, object]:
    supplier_id = getattr(row, "supplier_id", None)
    request_id = getattr(row, "request_id", None)
    row_kind = getattr(row, "kind", kind)
    return {
        "queue": kind,
        "id": row.id,
        "status": row.status,
        "kind": row_kind,
        "attempts": row.attempts,
        "supplier_id": supplier_id,
        "request_id": request_id,
        "last_error": sanitize_error(row.last_error),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_open_failures(session: AsyncSession, *, limit: int = 8) -> dict[str, list[dict[str, object]]]:
    inbox = (
        await session.execute(
            select(WebhookInbox)
            .where(WebhookInbox.status == "dead", WebhookInbox.resolved_at.is_(None))
            .order_by(WebhookInbox.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    outbox = (
        await session.execute(
            select(TelegramOutbox)
            .where(
                TelegramOutbox.status.in_(("dead", "uncertain")),
                TelegramOutbox.resolved_at.is_(None),
            )
            .order_by(TelegramOutbox.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "inbox": [_public_row("inbox", row) for row in inbox],
        "outbox": [_public_row("outbox", row) for row in outbox],
    }


async def replay_inbox(
    session: AsyncSession,
    row_id: int,
    *,
    actor_telegram_id: int | None,
) -> str:
    row = await session.get(WebhookInbox, row_id)
    if row is None or row.status != "dead" or row.resolved_at is not None:
        return "not_found"
    previous = row.status
    row.status = "pending"
    row.attempts = 0
    row.lease_owner = None
    row.leased_until = None
    row.next_attempt_at = datetime.now(UTC)
    row.last_error = None
    row.replay_of_id = row.id
    await record_audit(
        session,
        action="dlq_replay",
        entity_type="webhook_inbox",
        entity_id=str(row.id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"status": previous},
        new_state={"status": "pending"},
    )
    return "replayed"


async def replay_outbox(
    session: AsyncSession,
    row_id: int,
    *,
    actor_telegram_id: int | None,
    confirmed: bool,
) -> str:
    row = await session.get(TelegramOutbox, row_id)
    if row is None or row.status not in ("dead", "uncertain") or row.resolved_at is not None:
        return "not_found"
    if row.status == "uncertain" and row.kind in _UNSAFE_KINDS and not confirmed:
        raise ReplayNeedsConfirmation
    previous = row.status
    row.status = "pending"
    row.attempts = 0
    row.lease_owner = None
    row.leased_until = None
    row.next_attempt_at = datetime.now(UTC)
    row.last_error = None
    row.replay_of_id = row.id
    await record_audit(
        session,
        action="dlq_replay",
        entity_type="telegram_outbox",
        entity_id=str(row.id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"status": previous, "kind": row.kind},
        new_state={"status": "pending", "confirmed": confirmed},
    )
    return "replayed"


async def resolve_row(
    session: AsyncSession,
    *,
    queue: str,
    row_id: int,
    actor_telegram_id: int | None,
    reason: str,
) -> str:
    if queue == "inbox":
        inbox = await session.get(WebhookInbox, row_id)
        if inbox is None or inbox.resolved_at is not None:
            return "not_found"
        if inbox.status not in ("dead", "uncertain"):
            return "not_terminal"
        inbox.resolved_at = datetime.now(UTC)
        inbox.resolved_by = actor_telegram_id
        inbox.resolution_reason = reason[:200]
        await record_audit(
            session,
            action="dlq_resolve",
            entity_type=WebhookInbox.__tablename__,
            entity_id=str(inbox.id),
            actor_telegram_id=actor_telegram_id,
            previous_state={"status": inbox.status},
            new_state={"resolved": True, "reason": inbox.resolution_reason},
        )
        return "resolved"
    outbox = await session.get(TelegramOutbox, row_id)
    if outbox is None or outbox.resolved_at is not None:
        return "not_found"
    if outbox.status not in ("dead", "uncertain"):
        return "not_terminal"
    outbox.resolved_at = datetime.now(UTC)
    outbox.resolved_by = actor_telegram_id
    outbox.resolution_reason = reason[:200]
    await record_audit(
        session,
        action="dlq_resolve",
        entity_type=TelegramOutbox.__tablename__,
        entity_id=str(outbox.id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"status": outbox.status},
        new_state={"resolved": True, "reason": outbox.resolution_reason},
    )
    return "resolved"
