"""Supplier reply binding without Telegram reply."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, Quote, Request
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
