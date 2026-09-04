"""Owner-only /set_llm_price command."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Owner
from app.handlers.llm_billing_commands import handle_set_llm_price
from app.services.app_settings_service import LLM_TOPUP_PRICE_TEXT_KEY, get_setting
from tests.conftest import MockTelegramClient


@pytest.mark.asyncio
async def test_owner_updates_price_text(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner: Owner,
) -> None:
    message = {
        "from": {"id": seed_owner.telegram_id},
        "chat": {"id": seed_owner.telegram_id, "type": "private"},
        "text": "/set_llm_price 800 CNY / ~$0.16 за 1M input токенов",
    }

    result = await handle_set_llm_price(db_session, message, telegram=mock_telegram)

    assert result == "ok"
    assert len(mock_telegram.sent) == 1
    assert "800 CNY" in mock_telegram.sent[0][1]
    assert (await get_setting(db_session, LLM_TOPUP_PRICE_TEXT_KEY)) == "800 CNY / ~$0.16 за 1M input токенов"


@pytest.mark.asyncio
async def test_non_owner_ignored(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
) -> None:
    message = {
        "from": {"id": 999999999},
        "chat": {"id": 999999999, "type": "private"},
        "text": "/set_llm_price 1000 CNY",
    }

    result = await handle_set_llm_price(db_session, message, telegram=mock_telegram)

    assert result == "ignored"
    assert not mock_telegram.sent


@pytest.mark.asyncio
async def test_empty_arg_shows_usage_with_current_value(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "llm_topup_price_text", "500 CNY")
    message = {
        "from": {"id": seed_owner.telegram_id},
        "chat": {"id": seed_owner.telegram_id, "type": "private"},
        "text": "/set_llm_price",
    }

    result = await handle_set_llm_price(db_session, message, telegram=mock_telegram)

    assert result == "ok"
    assert "Использование" in mock_telegram.sent[0][1]
    assert "500 CNY" in mock_telegram.sent[0][1]


@pytest.mark.asyncio
async def test_long_text_rejected(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner: Owner,
) -> None:
    message = {
        "from": {"id": seed_owner.telegram_id},
        "chat": {"id": seed_owner.telegram_id, "type": "private"},
        "text": "/set_llm_price " + "x" * 600,
    }

    result = await handle_set_llm_price(db_session, message, telegram=mock_telegram)

    assert result == "ok"
    assert "Слишком длинное" in mock_telegram.sent[0][1]


@pytest.mark.asyncio
async def test_owner_can_update_even_with_dm_ok_false(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300300, name="Owner No DM", dm_ok=False)
    db_session.add(owner)
    await db_session.flush()

    message = {
        "from": {"id": owner.telegram_id},
        "chat": {"id": owner.telegram_id, "type": "private"},
        "text": "/set_llm_price 700 CNY",
    }

    result = await handle_set_llm_price(db_session, message, telegram=mock_telegram)

    assert result == "ok"
    assert (await get_setting(db_session, LLM_TOPUP_PRICE_TEXT_KEY)) == "700 CNY"
