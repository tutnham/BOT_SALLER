"""Billing reminder helpers — idempotency and static text generation."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BillingReminder
from app.services.app_settings_service import LLM_TOPUP_PRICE_TEXT_KEY, get_setting
from app.templates.messages_ru import render_template

if TYPE_CHECKING:
    from app.config import Settings

REMINDER_KIND = "llm_topup"


async def get_llm_topup_price_text(session: AsyncSession, settings: Settings) -> str:
    """Return runtime price text, falling back to env/bootstrap value."""
    db_value = await get_setting(session, LLM_TOPUP_PRICE_TEXT_KEY)
    if db_value is not None:
        return db_value
    if settings.llm_topup_price_text is not None:
        return settings.llm_topup_price_text
    return "не задана, обновите через /set_llm_price"


async def build_llm_billing_reminder_text(
    session: AsyncSession,
    settings: Settings,
) -> str | None:
    """Build a static reminder text. Returns ``None`` when payment URL is missing."""
    if settings.llm_topup_payment_url is None:
        logger.warning("LLM_TOPUP_PAYMENT_URL is not set; skip billing reminder")
        return None

    price_text = await get_llm_topup_price_text(session, settings)
    return render_template(
        "llm_billing_reminder",
        provider=settings.llm_provider_name,
        price_text=price_text,
        payment_url=settings.llm_topup_payment_url,
    )


async def reserve_reminder(session: AsyncSession, period_key: str) -> int | None:
    """Atomically reserve a period. Returns the row id for a fresh reservation,
    ``None`` when the period already exists."""
    stmt = (
        insert(BillingReminder)
        .values(
            kind=REMINDER_KIND,
            period_key=period_key,
            chat_ids=[],
        )
        .on_conflict_do_nothing(
            index_elements=["kind", "period_key"],
        )
        .returning(BillingReminder.id)
    )
    result = await session.execute(stmt)
    row_id = result.scalar_one_or_none()
    return int(row_id) if row_id is not None else None


async def release_reminder(session: AsyncSession, reminder_id: int) -> None:
    """Release a failed reservation so the next run can retry."""
    await session.execute(
        delete(BillingReminder).where(BillingReminder.id == reminder_id)
    )


async def record_reminder_sent(
    session: AsyncSession,
    reminder_id: int,
    chat_ids: list[int],
) -> None:
    """Persist the final destination list and sent timestamp."""
    await session.execute(
        update(BillingReminder)
        .where(BillingReminder.id == reminder_id)
        .values(chat_ids=chat_ids, sent_at=func.now())
    )


def period_key_for_datetime(dt: datetime, settings: Settings) -> str:
    """Return a YYYY-MM key for a timezone-aware datetime in the configured TZ."""
    tz = ZoneInfo(settings.tz)
    return dt.astimezone(tz).strftime("%Y-%m")


async def has_reminder_for_period(session: AsyncSession, period_key: str) -> bool:
    """Check whether a reservation already exists for the period."""
    result = await session.execute(
        select(BillingReminder.id).where(
            BillingReminder.kind == REMINDER_KIND,
            BillingReminder.period_key == period_key,
        )
    )
    return result.scalar_one_or_none() is not None
