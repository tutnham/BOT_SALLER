"""Direct unit tests for billing reminder helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BillingReminder
from app.services.app_settings_service import LLM_TOPUP_PRICE_TEXT_KEY, set_setting
from app.services.billing_service import (
    build_llm_billing_reminder_text,
    get_llm_topup_price_text,
    has_reminder_for_period,
    period_key_for_datetime,
    record_reminder_sent,
    release_reminder,
    reserve_reminder,
)


@pytest.fixture
def _billing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider_name", "DeepSeek")
    monkeypatch.setattr(
        settings, "llm_topup_payment_url", "https://platform.deepseek.com/top_up"
    )
    monkeypatch.setattr(settings, "llm_topup_price_text", "500 CNY")


@pytest.mark.asyncio
async def test_get_llm_topup_price_text_db_over_env(
    db_session: AsyncSession,
    _billing_settings: None,
) -> None:
    await set_setting(db_session, LLM_TOPUP_PRICE_TEXT_KEY, "800 CNY")
    text = await get_llm_topup_price_text(db_session, get_settings())
    assert text == "800 CNY"


@pytest.mark.asyncio
async def test_get_llm_topup_price_text_env_fallback(
    db_session: AsyncSession,
    _billing_settings: None,
) -> None:
    text = await get_llm_topup_price_text(db_session, get_settings())
    assert text == "500 CNY"


@pytest.mark.asyncio
async def test_build_reminder_text_skips_without_payment_url(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "llm_topup_payment_url", None)
    assert await build_llm_billing_reminder_text(db_session, get_settings()) is None


@pytest.mark.asyncio
async def test_reserve_reminder_is_idempotent(
    db_session: AsyncSession,
) -> None:
    first = await reserve_reminder(db_session, "2026-09")
    assert first is not None
    second = await reserve_reminder(db_session, "2026-09")
    assert second is None

    rows = (await db_session.execute(select(BillingReminder))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_release_reminder_allows_retry(
    db_session: AsyncSession,
) -> None:
    reminder_id = await reserve_reminder(db_session, "2026-09")
    assert reminder_id is not None
    await release_reminder(db_session, reminder_id)

    retry_id = await reserve_reminder(db_session, "2026-09")
    assert retry_id is not None
    assert retry_id != reminder_id


@pytest.mark.asyncio
async def test_record_reminder_sent_sets_chat_ids_and_sent_at(
    db_session: AsyncSession,
) -> None:
    reminder_id = await reserve_reminder(db_session, "2026-09")
    assert reminder_id is not None
    await record_reminder_sent(db_session, reminder_id, [100, 200])

    row = await db_session.get(BillingReminder, reminder_id)
    assert row is not None
    assert row.chat_ids == [100, 200]
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_has_reminder_for_period(
    db_session: AsyncSession,
) -> None:
    assert await has_reminder_for_period(db_session, "2026-09") is False
    await reserve_reminder(db_session, "2026-09")
    assert await has_reminder_for_period(db_session, "2026-09") is True


@pytest.mark.asyncio
async def test_period_key_uses_configured_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "tz", "Europe/Moscow")
    utc_dt = datetime(2026, 9, 1, 0, 30, tzinfo=UTC)
    assert period_key_for_datetime(utc_dt, get_settings()) == "2026-09"

    utc_dt = datetime(2026, 8, 31, 21, 30, tzinfo=UTC)
    assert period_key_for_datetime(utc_dt, get_settings()) == "2026-09"
