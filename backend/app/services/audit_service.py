"""Append-only operator audit. Rows have no foreign keys to business tables."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog
from app.services.audit_sanitize import sanitize_audit_state


async def record_admin_mutation(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_telegram_id: int | None = None,
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    outcome: str = "success",
) -> None:
    payload = {"outcome": outcome, **(new_state or {})}
    await record_audit(
        session,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_telegram_id=actor_telegram_id,
        previous_state=sanitize_audit_state(previous_state),
        new_state=sanitize_audit_state(payload),
        correlation_id=correlation_id,
    )


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_telegram_id: int | None = None,
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    session.add(
        AdminAuditLog(
            actor_telegram_id=actor_telegram_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            previous_state=sanitize_audit_state(previous_state),
            new_state=sanitize_audit_state(new_state),
            correlation_id=correlation_id,
        )
    )
    await session.flush()
