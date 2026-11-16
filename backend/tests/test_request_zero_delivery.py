"""A5: нулевая доставка RFQ → needs_recheck + кнопка повтора рассылки."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import RequestStatus, Supplier
from app.handlers.request_status_callbacks import handle_request_status_callback
from app.services.request_service import create_request
from app.telegram.client import TelegramSendError
from app.telegram.keyboards import CallbackData
from tests.conftest import MockTelegramClient

GROUP = -100888
LINE = "14 128GB Yellow iPhone"


class SupplierFailTelegramClient(MockTelegramClient):
    """Все отправки поставщикам (личные чаты, id > 0) падают."""

    async def send_message(self, chat_id, text, **kwargs):  # type: ignore[override]
        if chat_id > 0:
            raise TelegramSendError("supplier chat unreachable")
        return await super().send_message(chat_id, text, **kwargs)


@pytest.mark.asyncio
async def test_zero_delivery_marks_needs_recheck_with_retry_button(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    telegram = SupplierFailTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=GROUP,
        employee_id=seed_employee.id,
        source_text=LINE,
        telegram=telegram,
    )
    request = outcome.requests[0]
    assert outcome.sent_count == 0
    assert request.status is RequestStatus.needs_recheck

    zero_cards = [
        (text, markup)
        for chat_id, text, markup in telegram.sent
        if chat_id == GROUP and "RFQ не доставлен" in text
    ]
    assert zero_cards, "ожидалась карточка нулевой доставки в группе"
    _text, markup = zero_cards[0]
    assert markup is not None
    buttons = [
        btn.get("text", "")
        for row in markup.get("inline_keyboard", [])
        for btn in row
    ]
    assert "Повторить рассылку" in buttons


@pytest.mark.asyncio
async def test_resend_callback_restores_awaiting_answers(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    failing = SupplierFailTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=GROUP,
        employee_id=seed_employee.id,
        source_text=LINE,
        telegram=failing,
    )
    request = outcome.requests[0]
    assert request.status is RequestStatus.needs_recheck

    # Доставка восстановилась — сотрудник жмёт «Повторить рассылку»
    working = MockTelegramClient()
    data = CallbackData(namespace="reqstatus", action="resend", arg=request.id).encode()
    result = await handle_request_status_callback(
        db_session,
        {
            "id": "cb-resend",
            "from": {"id": seed_employee.telegram_id},
            "data": data,
            "message": {"chat": {"id": GROUP}},
        },
        telegram=working,
    )
    assert result == "ok"
    assert request.status is RequestStatus.awaiting_answers
    assert any(chat_id > 0 for chat_id, _text, _markup in working.sent)


@pytest.mark.asyncio
async def test_outbox_mode_queued_delivery_is_not_failure(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # В outbox-режиме RFQ ставится в очередь worker'а — это не нулевая доставка
    monkeypatch.setenv("OUTBOX_DELIVERY_ENABLED", "true")
    get_settings.cache_clear()
    try:
        telegram = MockTelegramClient()
        outcome = await create_request(
            db_session,
            group_chat_id=GROUP,
            employee_id=seed_employee.id,
            source_text=LINE,
            telegram=telegram,
        )
        request = outcome.requests[0]
        assert outcome.sent_count >= 1
        assert request.status is RequestStatus.awaiting_answers
        assert all("RFQ не доставлен" not in text for _, text, _ in telegram.sent)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unknown_category_stays_open_without_delivery_card(
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
        source_text="неизвестный товар без бренда",
        telegram=telegram,
    )
    request = outcome.requests[0]
    assert outcome.sent_count == 0
    assert request.status is RequestStatus.open
    assert all("RFQ не доставлен" not in text for _chat_id, text, _markup in telegram.sent)