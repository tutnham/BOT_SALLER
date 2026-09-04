"""Runtime-mutable settings stored in ``app_settings`` table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting

LLM_TOPUP_PRICE_TEXT_KEY = "llm_topup_price_text"


async def get_setting(session: AsyncSession, key: str) -> str | None:
    """Return a single setting value, or ``None`` if missing."""
    result = await session.execute(select(AppSetting.value).where(AppSetting.key == key))
    return result.scalar_one_or_none()


async def set_setting(
    session: AsyncSession,
    key: str,
    value: str,
    *,
    updated_by: int | None = None,
) -> AppSetting:
    """Upsert a setting atomically; returns the persisted row."""
    stmt = (
        insert(AppSetting)
        .values(key=key, value=value, updated_by=updated_by)
        .on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={
                "value": value,
                "updated_by": updated_by,
            },
        )
        .returning(AppSetting)
    )
    result = await session.execute(stmt)
    return result.scalar_one()
