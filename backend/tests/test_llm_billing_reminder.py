"""Monthly LLM billing reminder runner."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AppSetting, BillingReminder, Owner
from app.jobs import run_llm_billing_reminder
from app.services.app_settings_service import LLM_TOPUP_PRICE_TEXT_KEY, set_setting
from app.telegram.client import TelegramSendError
from tests.conftest import MockTelegramClient


@pytest.fixture
def _monkeypatch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider_name", "DeepSeek")
    monkeypatch.setattr(settings, "llm_topup_payment_url", "https://platform.deepseek.com/top_up")
    monkeypatch.setattr(settings, "llm_topup_price_text", "500 CNY")
    monkeypatch.setattr(settings, "llm_billing_reminder_chat_id", None)
    monkeypatch.setattr(settings, "admin_alert_chat_id", 400400400)


@pytest.mark.asyncio
async def test_reminder_uses_db_price_over_env(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    _monkeypatch_settings: None,
) -> None:
    owner = Owner(telegram_id=100100100, name="Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await set_setting(db_session, LLM_TOPUP_PRICE_TEXT_KEY, "800 CNY")

    result = await run_llm_billing_reminder(db_session, mock_telegram)

    assert result["status"] == "ok"
    assert result["sent"] == 1
    assert mock_telegram.sent[0][0] == 100100100
    assert "800 CNY" in mock_telegram.sent[0][1]
    assert "platform.deepseek.com" in mock_telegram.sent[0][1]


@pytest.mark.asyncio
async def test_reminder_records_sent_at_and_chat_ids(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    _monkeypatch_settings: None,
) -> None:
    owner = Owner(telegram_id=100100100, name="Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    result = await run_llm_billing_reminder(db_session, mock_telegram)

    assert result["status"] == "ok"
    row = await db_session.get(BillingReminder, 1)
    assert row is not None
    assert row.sent_at is not None
    assert row.chat_ids == [100100100]


@pytest.mark.asyncio
async def test_reminder_skips_without_payment_url(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_topup_payment_url", None)
    monkeypatch.setattr(settings, "admin_alert_chat_id", 400400400)

    result = await run_llm_billing_reminder(db_session, mock_telegram)

    assert result == {"status": "skipped", "reason": "payment_url_unset"}
    assert not mock_telegram.sent


@pytest.mark.asyncio
async def test_reminder_is_idempotent(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    _monkeypatch_settings: None,
) -> None:
    owner = Owner(telegram_id=100100100, name="Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    first = await run_llm_billing_reminder(db_session, mock_telegram)
    assert first["status"] == "ok"

    second = await run_llm_billing_reminder(db_session, mock_telegram)
    assert second == {"status": "already_sent", "period_key": first["period_key"]}
    assert len(mock_telegram.sent) == 1


@pytest.mark.asyncio
async def test_reminder_releases_on_total_failure(
    db_session: AsyncSession,
    _monkeypatch_settings: None,
) -> None:
    owner = Owner(telegram_id=100100100, name="Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    class _FailingTelegram:
        sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> int:
            raise TelegramSendError("network")

    telegram = _FailingTelegram()

    result = await run_llm_billing_reminder(db_session, telegram)
    assert result["status"] == "degraded"

    # Reservation should be released so a retry can proceed.
    row = await db_session.get(BillingReminder, 1)
    assert row is None


@pytest.mark.asyncio
async def test_reminder_destinations_chain(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_billing_reminder_chat_id", 700700700)
    monkeypatch.setattr(settings, "admin_alert_chat_id", 400400400)

    # dm_ok owners beat fallback chat.
    owner = Owner(telegram_id=100100100, name="Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    result = await run_llm_billing_reminder(db_session, mock_telegram)
    assert result["sent"] == 1
    assert mock_telegram.sent[0][0] == 100100100


@pytest.mark.asyncio
async def test_reminder_falls_back_to_configured_chat_id(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_billing_reminder_chat_id", 700700700)
    monkeypatch.setattr(settings, "admin_alert_chat_id", None)

    result = await run_llm_billing_reminder(db_session, mock_telegram)
    assert result["sent"] == 1
    assert mock_telegram.sent[0][0] == 700700700


@pytest.mark.asyncio
async def test_reminder_period_key_in_configured_timezone(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    _monkeypatch_settings: None,
) -> None:
    settings = get_settings()

    # 2026-08-31 23:30 UTC == 2026-09-01 02:30 MSK
    msk_just_after_midnight = datetime(2026, 9, 1, 2, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    utc_before_midnight = msk_just_after_midnight.astimezone(timezone.utc)

    with patch("app.jobs.datetime") as mock_dt:
        mock_dt.now.return_value = utc_before_midnight
        result = await run_llm_billing_reminder(db_session, mock_telegram)

    assert result["period_key"] == "2026-09"
    assert (await db_session.get(BillingReminder, 1)).period_key == "2026-09"


@pytest.mark.asyncio
async def test_reminder_no_destinations(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_billing_reminder_chat_id", None)
    monkeypatch.setattr(settings, "admin_alert_chat_id", None)

    result = await run_llm_billing_reminder(db_session, mock_telegram)
    assert result == {"status": "skipped", "reason": "no_destinations"}
