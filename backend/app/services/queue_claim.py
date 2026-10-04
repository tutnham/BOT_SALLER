"""Shared lease claim helpers (FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def claim_rows(
    session: AsyncSession,
    model: Any,
    *,
    pending_status: str,
    processing_status: str,
    lease_seconds: int,
    batch_size: int,
    worker_id: str | None = None,
) -> list[Any]:
    """Claim up to ``batch_size`` rows ready for processing."""
    owner = worker_id or f"worker-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=lease_seconds)

    subq = (
        select(model.id)
        .where(
            model.status == pending_status,  # type: ignore[attr-defined]
            model.next_attempt_at <= now,  # type: ignore[attr-defined]
            or_(
                model.leased_until.is_(None),  # type: ignore[attr-defined]
                model.leased_until < now,  # type: ignore[attr-defined]
            ),
        )
        .order_by(model.id.asc())  # type: ignore[attr-defined]
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    ids = list((await session.execute(subq)).scalars().all())
    if not ids:
        return []

    await session.execute(
        update(model)
        .where(model.id.in_(ids))  # type: ignore[attr-defined]
        .values(
            status=processing_status,
            lease_owner=owner,
            leased_until=lease_until,
        )
    )
    await session.flush()

    result = await session.execute(
        select(model).where(
            and_(
                model.id.in_(ids),  # type: ignore[attr-defined]
                model.lease_owner == owner,  # type: ignore[attr-defined]
            )
        )
    )
    return list(result.scalars().all())
