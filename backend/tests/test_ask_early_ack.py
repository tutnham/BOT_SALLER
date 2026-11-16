"""A3: раннее подтверждение приёма мультипозиционной заявки /ask."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Supplier
from app.services.request_service import create_request
from app.telegram.client import TelegramSendError
from tests.conftest import MockTelegramClient

GROUP = -100777
MULTI_TEXT = "14 128GB Yellow iPhone\n15 256GB Black iPhone"
ACK_MARK = "Заявка принята, позиций: 2"


@pytest.mark.asyncio
async def test_multi_position_ask_sends_early_ack(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    telegram = MockTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=GROUP,
        employee_id=seed_employee.id,
        source_text=MULTI_TEXT,
        telegram=telegram,
    )
    assert len(outcome.requests) == 2

    group_texts = [text for chat_id, text, _ in telegram.sent if chat_id == GROUP]
    assert group_texts, "ожидались сообщения в группу клиента"
    # Подтверждение приёма — первое сообщение, до нормализации/рассылки
    assert ACK_MARK in group_texts[0]


@pytest.mark.asyncio
async def test_single_position_ask_has_no_ack(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    telegram = MockTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=GROUP,
        employee_id=seed_employee.id,
        source_text="14 128GB Yellow iPhone",
        telegram=telegram,
    )
    assert len(outcome.requests) == 1
    assert all("Заявка принята" not in text for _, text, _ in telegram.sent)


@pytest.mark.asyncio
async def test_ack_failure_does_not_break_request(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    class AckFailTelegramClient(MockTelegramClient):
        async def send_message(self, chat_id, text, **kwargs):  # type: ignore[override]
            if "Заявка принята" in text:
                raise TelegramSendError("ack send failed")
            return await super().send_message(chat_id, text, **kwargs)

    telegram = AckFailTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=GROUP,
        employee_id=seed_employee.id,
        source_text=MULTI_TEXT,
        telegram=telegram,
    )
    # Сбой подтверждения не мешает созданию заявок и рассылке RFQ
    assert len(outcome.requests) == 2
    assert outcome.sent_count >= 1