"""update_log idempotency helpers."""

import pytest
from app.db.models import UpdateLog
from app.utils.idempotency import is_duplicate_update, mark_update_processed
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_mark_update_processed_first_then_duplicate(
    db_session: AsyncSession,
) -> None:
    tg_update_id = 42_001

    assert await is_duplicate_update(db_session, tg_update_id) is False

    first = await mark_update_processed(db_session, tg_update_id)
    assert first is False  # newly inserted
    assert await is_duplicate_update(db_session, tg_update_id) is True

    second = await mark_update_processed(db_session, tg_update_id)
    assert second is True  # duplicate

    count = await db_session.scalar(
        select(func.count()).select_from(UpdateLog).where(
            UpdateLog.tg_update_id == tg_update_id
        )
    )
    assert count == 1
