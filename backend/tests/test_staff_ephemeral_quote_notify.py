"""Групповые уведомления о ценах: публичная первая карточка, далее ephemeral персоналу."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote
from app.handlers.supplier_messages import handle_reply, process_bound_supplier_reply
from app.services.request_service import create_request
from app.services.staff_ephemeral_notify import notify_staff_ephemeral_in_group
from app.telegram.client import TelegramSendError
from tests.conftest import MockTelegramClient

pytestmark = pytest.mark.usefixtures("supplier_single_candidate_auto_bind")


def _supplier_message(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


def _group_price_sends(
    telegram: MockTelegramClient,
    group_chat_id: int,
) -> list[tuple[str, dict[str, Any] | None]]:
    out: list[tuple[str, dict[str, Any] | None]] = []
    for (chat_id, text, _), ephemeral in zip(
        telegram.sent, telegram.ephemeral_message_parameters
    ):
        if chat_id != group_chat_id:
            continue
        if "По вашему запросу" in text or (
            "Заявка #" in text and "Закуп:" in text
        ):
            out.append((text, ephemeral))
    return out


class EphemeralFanoutFailTelegram(MockTelegramClient):
    """Падает на одном receiver_user_id, остальные доставляются."""

    def __init__(self, fail_receiver_user_id: int) -> None:
        super().__init__()
        self.fail_receiver_user_id = fail_receiver_user_id

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        business_connection_id: str | None = None,
        ephemeral_message_parameters: dict[str, Any] | None = None,
    ) -> int:
        receiver = (
            ephemeral_message_parameters.get("receiver_user_id")
            if ephemeral_message_parameters
            else None
        )
        if receiver == self.fail_receiver_user_id:
            raise TelegramSendError("test_ephemeral_receiver_failed")
        return await super().send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
            ephemeral_message_parameters=ephemeral_message_parameters,
        )


@pytest.mark.asyncio
async def test_first_supplier_quote_is_public_group_message(
    db_session: AsyncSession,
    seed_employee,
    seed_owner,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 601, "85000"),
        telegram=mock_telegram,
    )

    price_sends = _group_price_sends(mock_telegram, seed_client_group.chat_id)
    assert len(price_sends) == 1
    text, ephemeral = price_sends[0]
    assert ephemeral is None
    assert "По вашему запросу" in text
    assert "Закуп:" not in text


@pytest.mark.asyncio
async def test_second_supplier_quote_ephemeral_fanout_to_staff(
    db_session: AsyncSession,
    seed_employee,
    seed_owner,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    seed_employee_telegram_id: int,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 silver",
        telegram=mock_telegram,
    )
    first, second = seed_suppliers[0], seed_suppliers[1]
    assert first.telegram_id is not None and second.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(first.telegram_id, 701, "86000"),
        telegram=mock_telegram,
    )
    sent_before = len(mock_telegram.sent)

    await handle_reply(
        db_session,
        _supplier_message(second.telegram_id, 702, "84000"),
        telegram=mock_telegram,
    )

    new_ephemeral = [
        ephemeral
        for ephemeral in mock_telegram.ephemeral_message_parameters[sent_before:]
        if ephemeral is not None
    ]
    assert len(new_ephemeral) == 2
    receivers = {int(item["receiver_user_id"]) for item in new_ephemeral}
    assert receivers == {seed_employee_telegram_id, seed_owner.telegram_id}

    staff_texts = [
        text
        for chat_id, text, _ in mock_telegram.sent[sent_before:]
        if chat_id == seed_client_group.chat_id
    ]
    assert any("Закуп:" in text for text in staff_texts)

    quotes = (
        await db_session.execute(select(Quote).where(Quote.request_id == request.id))
    ).scalars().all()
    assert len(quotes) == 2


@pytest.mark.asyncio
async def test_ephemeral_fanout_continues_after_one_receiver_fails(
    db_session: AsyncSession,
    seed_employee,
    seed_owner,
    seed_client_group,
    seed_employee_telegram_id: int,
) -> None:
    telegram = EphemeralFanoutFailTelegram(seed_employee_telegram_id)
    delivered = await notify_staff_ephemeral_in_group(
        db_session,
        telegram,
        group_chat_id=seed_client_group.chat_id,
        text="staff-only",
        request_id=1,
    )
    assert delivered == 1
    assert len(telegram.sent) == 1
    assert telegram.ephemeral_message_parameters[0] is not None
    assert (
        telegram.ephemeral_message_parameters[0]["receiver_user_id"]
        == seed_owner.telegram_id
    )


@pytest.mark.asyncio
async def test_quote_update_uses_staff_ephemeral(
    db_session: AsyncSession,
    seed_employee,
    seed_owner,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 801, "85000"),
        telegram=mock_telegram,
    )
    sent_before = len(mock_telegram.sent)

    from app.db.models import MessageIn

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None

    second_in = MessageIn(
        supplier_id=supplier.id,
        chat_id=supplier.telegram_id,
        tg_message_id=802,
        raw_text="82000",
        request_id=request.id,
        bind_status="bound",
    )
    db_session.add(second_in)
    await db_session.flush()

    await process_bound_supplier_reply(
        db_session,
        request=request,
        supplier=supplier,
        raw_text="82000",
        message_in=second_in,
        telegram=mock_telegram,
        business_connection_id=None,
        chat_id=supplier.telegram_id,
        bind_method="reply",
    )
    await db_session.refresh(quote)
    assert quote.price_initial == 82000

    new_ephemeral = [
        item
        for item in mock_telegram.ephemeral_message_parameters[sent_before:]
        if item is not None
    ]
    assert len(new_ephemeral) >= 2