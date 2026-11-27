"""Callback-обработок оффера: права и удаление кнопок после нажатия."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Request, Supplier
from app.handlers.channel_price_callbacks import handle_channel_price_callback
from app.llm.client import set_llm_client
from app.telegram.keyboards import CallbackData
from tests.conftest import MockLLMClient, MockTelegramClient
from tests.test_ask_channel_price_offer_confirm import (
    _client_outbox,
    _seed_offer,
)


def _callback(*, telegram_id: int, request_id: int, action: str) -> dict:
    return {
        "id": "cb-1",
        "from": {"id": telegram_id},
        "data": CallbackData(
            namespace="chanprice", action=action, arg=request_id
        ).encode(),
        "message": {"message_id": 555, "chat": {"id": -1001234567890}},
    }


@pytest.mark.asyncio
async def test_stranger_gets_access_denied_without_writes(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    seed_employee,
    seed_owner,
    seed_group_chat_id: int,
    channel_price_flags_on: None,
) -> None:
    set_llm_client(mock_llm)
    try:
        request = await _seed_offer(
            db_session,
            mock_llm,
            seed_channel_supplier,
            seed_employee.id,
            seed_group_chat_id,
            mock_telegram,
        )

        status = await handle_channel_price_callback(
            db_session,
            _callback(telegram_id=999888777, request_id=request.id, action="confirm"),
            telegram=mock_telegram,
        )
        assert status == "ignored"
        assert mock_telegram.answer_callbacks[-1][1] == "Нет доступа"
        assert await _client_outbox(db_session) == []
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_approver_confirm_publishes_and_clears_buttons(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    seed_employee,
    seed_owner,
    seed_group_chat_id: int,
    channel_price_flags_on: None,
) -> None:
    set_llm_client(mock_llm)
    try:
        request: Request = await _seed_offer(
            db_session,
            mock_llm,
            seed_channel_supplier,
            seed_employee.id,
            seed_group_chat_id,
            mock_telegram,
        )

        status = await handle_channel_price_callback(
            db_session,
            _callback(
                telegram_id=seed_owner.telegram_id, request_id=request.id, action="confirm"
            ),
            telegram=mock_telegram,
        )
        assert status == "ok"
        rows = await _client_outbox(db_session)
        assert len(rows) == 1
        # Карточка переоформлена без клавиатуры — повторного нажатия нет.
        assert mock_telegram.edited[-1][3] is None

        await handle_channel_price_callback(
            db_session,
            _callback(
                telegram_id=seed_owner.telegram_id, request_id=request.id, action="confirm"
            ),
            telegram=mock_telegram,
        )
        assert len(await _client_outbox(db_session)) == 1
    finally:
        set_llm_client(None)
