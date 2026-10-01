"""Supplier reply binding without Telegram reply."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, Owner, Quote, Request, RequestStatus
from app.handlers.supplier_messages import (
    handle_reply,
    manual_bind_message,
    unbind_message,
)
from app.llm.client import set_llm_client
from app.services.request_service import create_request
from tests.conftest import MockLLMClient, MockTelegramClient, next_tg_update_id


@pytest.mark.asyncio
async def test_single_open_request_binds_without_reply(
    db_session: AsyncSession,
    seed_employee,
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

    status = await handle_reply(
        db_session,
        {
            "message_id": 501,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "85000",
        },
        telegram=mock_telegram,
    )
    assert status == "ok"
    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == 85000


@pytest.mark.asyncio
async def test_greeting_does_not_create_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        {
            "message_id": 502,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "добрый день",
        },
        telegram=mock_telegram,
    )
    count = len(
        (
            await db_session.execute(select(Quote))
        ).scalars().all()
    )
    assert count == 0


@pytest.mark.asyncio
async def test_duplicate_message_id_idempotent(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 16 128",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    payload = {
        "message_id": 503,
        "chat": {"id": supplier.telegram_id, "type": "private"},
        "from": {"id": supplier.telegram_id},
        "text": "70000",
    }
    await handle_reply(db_session, payload, telegram=mock_telegram)
    await handle_reply(db_session, payload, telegram=mock_telegram)
    quotes = (
        await db_session.execute(
            select(Quote).where(
                Quote.request_id == request.id,
                Quote.supplier_id == supplier.id,
            )
        )
    ).scalars().all()
    assert len(quotes) == 1
    rows = (
        await db_session.execute(
            select(MessageIn).where(
                MessageIn.chat_id == supplier.telegram_id,
                MessageIn.tg_message_id == 503,
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_bare_prices_bind_in_send_order(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    first, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    second, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        {
            "message_id": 601,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "111000",
        },
        telegram=mock_telegram,
    )
    await handle_reply(
        db_session,
        {
            "message_id": 602,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "222000",
        },
        telegram=mock_telegram,
    )

    first_quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == first.id,
            Quote.supplier_id == supplier.id,
        )
    )
    second_quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == second.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert first_quote is not None and first_quote.price_initial == 111000
    assert second_quote is not None and second_quote.price_initial == 222000
    first_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 601)
    )
    assert first_in is not None
    assert first_in.bind_method == "order"


@pytest.mark.asyncio
async def test_named_model_unbound_alert_has_bind_buttons(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300399, name="Alert Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        {
            "message_id": 603,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "iPhone 18 Pro 117900",
        },
        telegram=mock_telegram,
    )

    alert = next(
        (item for item in mock_telegram.sent if item[0] == owner.telegram_id),
        None,
    )
    assert alert is not None
    markup = alert[2]
    assert markup is not None
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    ]
    assert any("bind_pick" in data for data in callbacks)
    assert any("bind_ignore" in data for data in callbacks)
    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []


def _supplier_message(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


def _callback_data(markup: dict) -> list[str]:
    return [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    ]


@pytest.mark.asyncio
async def test_bare_price_binds_oldest_and_owner_gets_card(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)
    try:
        owner = Owner(telegram_id=300300398, name="Card Owner", dm_ok=True)
        db_session.add(owner)
        await db_session.flush()

        first, _ = await create_request(
            db_session,
            group_chat_id=seed_client_group.chat_id,
            employee_id=seed_employee.id,
            source_text="iPhone 17 Pro 256",
            telegram=mock_telegram,
        )
        second, _ = await create_request(
            db_session,
            group_chat_id=seed_client_group.chat_id,
            employee_id=seed_employee.id,
            source_text="iPhone 17 Pro Max 256",
            telegram=mock_telegram,
        )
        supplier = seed_suppliers[0]
        assert supplier.telegram_id is not None
        mock_telegram.sent.clear()

        await handle_reply(
            db_session,
            _supplier_message(supplier.telegram_id, 701, "117900"),
            telegram=mock_telegram,
        )

        quote = await db_session.scalar(
            select(Quote).where(
                Quote.request_id == first.id,
                Quote.supplier_id == supplier.id,
            )
        )
        assert quote is not None
        assert quote.price_initial == 117900
        assert quote.price_final is not None
        assert quote.price_final > quote.price_initial

        group_messages = [
            text
            for chat_id, text, _markup in mock_telegram.sent
            if chat_id == seed_client_group.chat_id
        ]
        assert any(f"#{first.id}" in text for text in group_messages)
        assert any(str(quote.price_final) in text for text in group_messages)

        card = next(
            (item for item in mock_telegram.sent if item[0] == owner.telegram_id),
            None,
        )
        assert card is not None
        assert f"#{first.id}" in card[1]
        assert "117900" in card[1]
        callbacks = _callback_data(card[2])
        assert any(
            "bind_pick" in data and data.endswith(f":{second.id}")
            for data in callbacks
        )
        assert any("bind_unbind" in data for data in callbacks)

        assert mock_llm.classify_supplier_reply_calls == 0
        assert mock_llm.parse_calls == 0
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_single_candidate_bare_price_sends_no_owner_card(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300397, name="Quiet Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 702, "99000"),
        telegram=mock_telegram,
    )

    owner_messages = [
        item for item in mock_telegram.sent if item[0] == owner.telegram_id
    ]
    assert owner_messages == []


@pytest.mark.asyncio
async def test_rebind_moves_quote_and_notifies_old_group(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    first, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    second, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 703, "117900"),
        telegram=mock_telegram,
    )
    message_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 703)
    )
    assert message_in is not None
    assert message_in.request_id == first.id
    await db_session.refresh(first)
    assert first.status is RequestStatus.priced
    mock_telegram.sent.clear()

    result = await manual_bind_message(
        db_session,
        message_in_id=message_in.id,
        request_id=second.id,
        telegram=mock_telegram,
    )
    assert result == "ok"

    old_quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == first.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert old_quote is None
    new_quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == second.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert new_quote is not None
    assert new_quote.price_initial == 117900

    await db_session.refresh(first)
    assert first.status is RequestStatus.awaiting_answers

    group_texts = [
        text
        for chat_id, text, _markup in mock_telegram.sent
        if chat_id == seed_client_group.chat_id
    ]
    assert any("неактуально" in text and f"#{first.id}" in text for text in group_texts)
    assert any(f"#{second.id}" in text for text in group_texts)


@pytest.mark.asyncio
async def test_rebind_keeps_quote_from_newer_message(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    first, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    second, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 704, "111000"),
        telegram=mock_telegram,
    )
    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 705, "222000"),
        telegram=mock_telegram,
    )
    # Both requests quoted now: third bare price stays unbound, bind it manually.
    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 706, "115000"),
        telegram=mock_telegram,
    )
    third_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 706)
    )
    assert third_in is not None
    assert third_in.request_id is None
    result = await manual_bind_message(
        db_session,
        message_in_id=third_in.id,
        request_id=first.id,
        telegram=mock_telegram,
    )
    assert result == "ok"

    # Now move the OLD message (704) to the second request: the quote on the
    # first request came from message 706 and must survive.
    first_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 704)
    )
    assert first_in is not None
    result = await manual_bind_message(
        db_session,
        message_in_id=first_in.id,
        request_id=second.id,
        telegram=mock_telegram,
    )
    assert result == "ok"

    first_quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == first.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert first_quote is not None
    assert first_quote.price_initial == 115000


@pytest.mark.asyncio
async def test_repeat_bind_same_request_is_noop(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    first, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 707, "117900"),
        telegram=mock_telegram,
    )
    message_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 707)
    )
    assert message_in is not None
    mock_telegram.sent.clear()

    result = await manual_bind_message(
        db_session,
        message_in_id=message_in.id,
        request_id=first.id,
        telegram=mock_telegram,
    )
    assert result == "already_bound"
    assert mock_telegram.sent == []


@pytest.mark.asyncio
async def test_unbind_withdraws_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    first, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 708, "117900"),
        telegram=mock_telegram,
    )
    message_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 708)
    )
    assert message_in is not None
    mock_telegram.sent.clear()

    result = await unbind_message(
        db_session, message_in_id=message_in.id, telegram=mock_telegram
    )
    assert result == "ok"
    await db_session.refresh(message_in)
    assert message_in.request_id is None
    assert message_in.bind_status == "unbound"

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == first.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is None
    group_texts = [
        text
        for chat_id, text, _markup in mock_telegram.sent
        if chat_id == seed_client_group.chat_id
    ]
    assert any("неактуально" in text for text in group_texts)


@pytest.mark.asyncio
async def test_bind_ignore_marks_message_ignored(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        _supplier_message(supplier.telegram_id, 709, "iPhone 18 Pro 117900"),
        telegram=mock_telegram,
    )
    message_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 709)
    )
    assert message_in is not None
    assert message_in.request_id is None

    result = await unbind_message(
        db_session,
        message_in_id=message_in.id,
        telegram=mock_telegram,
        status="ignored",
    )
    assert result == "ok"
    await db_session.refresh(message_in)
    assert message_in.bind_status == "ignored"
