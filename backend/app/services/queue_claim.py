"""Shared lease claim helpers (FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

ExpiredHandler = Callable[[Any], Awaitable[bool]]


async def claim_rows(
    session: AsyncSession,
    model: Any,
    *,
    pending_status: str,
    processing_status: str,
    lease_seconds: int,
    batch_size: int,
    worker_id: str | None = None,
    on_expired: ExpiredHandler | None = None,
) -> list[Any]:
    """Claim pending rows and expired ``processing`` leases in one lock."""
    owner = worker_id or f"worker-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=lease_seconds)

    subq = (
        select(model.id)
        .where(
            or_(
                and_(
                    model.status == pending_status,  # type: ignore[attr-defined]
                    model.next_attempt_at <= now,  # type: ignore[attr-defined]
                    or_(
                        model.leased_until.is_(None),  # type: ignore[attr-defined]
                        model.leased_until < now,  # type: ignore[attr-defined]
                    ),
                ),
                and_(
                    model.status == processing_status,  # type: ignore[attr-defined]
                    model.leased_until.is_not(None),  # type: ignore[attr-defined]
                    model.leased_until < now,  # type: ignore[attr-defined]
                ),
            )
        )
        .order_by(model.id.asc())  # type: ignore[attr-defined]
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    ids = list((await session.execute(subq)).scalars().all())
    if not ids:
        return []

    locked = list(
        (
            await session.execute(
                select(model).where(model.id.in_(ids))  # type: ignore[attr-defined]
            )
        ).scalars().all()
    )
    claimed: list[Any] = []
    for row in locked:
        if row.status == processing_status and on_expired is not None:
            keep = await on_expired(row)
            if not keep:
                continue
        row.status = processing_status
        row.lease_owner = owner
        row.leased_until = lease_until
        claimed.append(row)
    await session.flush()
    return claimed
