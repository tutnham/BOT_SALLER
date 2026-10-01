"""Supplier reply binding without Telegram reply."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, Owner, Quote, Request
from app.handlers.supplier_messages import handle_reply
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient, next_tg_update_id


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
    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []
