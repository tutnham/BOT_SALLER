"""A1: сбой отправки уведомления не откатывает котировку поставщика."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, Quote, RequestStatus
from app.handlers.supplier_messages import handle_reply, process_bound_supplier_reply
from app.services.request_service import create_request
from app.telegram.client import TelegramSendError
from tests.conftest import MockTelegramClient

pytestmark = pytest.mark.usefixtures("supplier_single_candidate_auto_bind")


class GroupFailTelegramClient(MockTelegramClient):
    """Отправка в клиентскую группу падает, остальные чаты работают."""

    def __init__(self, fail_chat_id: int) -> None:
        super().__init__()
        self.fail_chat_id = fail_chat_id

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        business_connection_id: str | None = None,
    ) -> int:
        if chat_id == self.fail_chat_id:
            raise TelegramSendError("test_group_send_failed")
        return await super().send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
        )


def _supplier_message(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


@pytest.mark.asyncio
async def test_group_notify_failure_keeps_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
) -> None:
    telegram = GroupFailTelegramClient(seed_client_group.chat_id)
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 silver",
        telegram=telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 501, "85000"),
        telegram=telegram,
    )

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == 85000
    message_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 501)
    )
    assert message_in is not None
    assert message_in.bind_status == "bound"


@pytest.mark.asyncio
async def test_closed_request_notify_failure_does_not_raise(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
) -> None:
    telegram = GroupFailTelegramClient(seed_client_group.chat_id)
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 silver",
        telegram=telegram,
    )
    request.status = RequestStatus.closed
    await db_session.flush()
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    message_in = MessageIn(
        supplier_id=supplier.id,
        chat_id=supplier.telegram_id,
        tg_message_id=777,
        raw_text="85000",
        request_id=request.id,
        bind_status="bound",
    )
    db_session.add(message_in)
    await db_session.flush()

    result = await process_bound_supplier_reply(
        db_session,
        request=request,
        supplier=supplier,
        raw_text="85000",
        message_in=message_in,
        telegram=telegram,
        business_connection_id=None,
        chat_id=supplier.telegram_id,
        bind_method="reply",
    )
    assert result is None