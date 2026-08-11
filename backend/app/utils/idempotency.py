"""Idempotency via ``update_log.tg_update_id`` (TECH DOC §7.1, §13)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UpdateLog


async def is_duplicate_update(session: AsyncSession, tg_update_id: int) -> bool:
    """Return True if ``tg_update_id`` was already processed."""
    result = await session.execute(
        select(UpdateLog.id).where(UpdateLog.tg_update_id == tg_update_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def mark_update_processed(session: AsyncSession, tg_update_id: int) -> bool:
    """
    Record ``tg_update_id`` in ``update_log``.

    Returns:
        True  — already present (duplicate; no new row).
        False — newly inserted (first time processing this update).

    Uses INSERT ... ON CONFLICT DO NOTHING for race-safe uniqueness.
    """
    stmt = (
        insert(UpdateLog)
        .values(tg_update_id=tg_update_id)
        .on_conflict_do_nothing(index_elements=["tg_update_id"])
        .returning(UpdateLog.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await session.flush()
    # None means conflict → duplicate
    return inserted_id is None
