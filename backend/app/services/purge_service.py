"""Hard-delete requests and related rows (owner-only purge)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, MessageIn, MessageOut, Quote, Request, RequestStatus

DEFAULT_PURGE_OLD_LIMIT = 50


async def _delete_request_children(
    session: AsyncSession,
    request_ids: list[int],
) -> None:
    """Delete child rows for one or more request ids in FK-safe order."""
    if not request_ids:
        return
    await session.execute(
        delete(MessageOut).where(MessageOut.request_id.in_(request_ids))
    )
    await session.execute(
        delete(MessageIn).where(MessageIn.request_id.in_(request_ids))
    )
    await session.execute(delete(Quote).where(Quote.request_id.in_(request_ids)))
    await session.execute(delete(Deal).where(Deal.request_id.in_(request_ids)))


async def hard_delete_request(session: AsyncSession, request_id: int) -> bool:
    """
    Hard-delete request and all dependent rows in one transaction.

    Returns:
        True if request existed and was deleted, False if not found.
    """
    request = await session.get(Request, request_id)
    if request is None:
        return False

    await _delete_request_children(session, [request_id])
    await session.execute(delete(Request).where(Request.id == request_id))
    await session.flush()
    return True


async def purge_old_requests(
    session: AsyncSession,
    *,
    days: int,
    limit: int = DEFAULT_PURGE_OLD_LIMIT,
    dry_run: bool = True,
) -> tuple[int, list[int]]:
    """
    Find or delete cancelled/closed requests older than ``days``.

    Returns:
        (count, list of request ids matched or deleted)
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(Request.id)
        .where(
            Request.status.in_((RequestStatus.cancelled, RequestStatus.closed)),
            Request.created_at < cutoff,
        )
        .order_by(Request.created_at.asc())
        .limit(limit)
    )
    request_ids = [int(row) for row in result.scalars().all()]
    if dry_run or not request_ids:
        return len(request_ids), request_ids

    await _delete_request_children(session, request_ids)
    await session.execute(delete(Request).where(Request.id.in_(request_ids)))
    await session.flush()
    return len(request_ids), request_ids
