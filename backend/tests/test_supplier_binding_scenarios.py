"""Additional supplier binding scenarios (route, explicit id, identity)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MessageIn,
    MessageKind,
    MessageOut,
    Quote,
    Request,
    RequestStatus,
)
from app.handlers.supplier_messages import handle_reply
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient


def _dm(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


@pytest.mark.asyncio
async def test_explicit_request_id_rejects_foreign_request(
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
        _dm(supplier.telegram_id, 10001, f"на #{second.id + 999} цена 117900"),
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(select(Quote).where(Quote.supplier_id == supplier.id))
    assert quote is None


@pytest.mark.asyncio
async def test_business_and_bot_same_chat_message_id_both_stored(
    db_session: AsyncSession,
    seed_suppliers,
) -> None:
    supplier = seed_suppliers[0]
    db_session.add(
        MessageIn(
            supplier_id=supplier.id,
            tg_message_id=4242,
            chat_id=supplier.telegram_id or 0,
            raw_text="bot",
            business_connection_id=None,
        )
    )
    db_session.add(
        MessageIn(
            supplier_id=supplier.id,
            tg_message_id=4242,
            chat_id=supplier.telegram_id or 0,
            raw_text="business",
            business_connection_id="bc_test",
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_duplicate_bot_route_message_id_rejected(
    db_session: AsyncSession,
    seed_suppliers,
) -> None:
    supplier = seed_suppliers[0]
    chat_id = supplier.telegram_id or 0
    db_session.add(
        MessageIn(
            supplier_id=supplier.id,
            tg_message_id=5252,
            chat_id=chat_id,
            raw_text="first",
        )
    )
    await db_session.flush()
    db_session.add(
        MessageIn(
            supplier_id=supplier.id,
            tg_message_id=5252,
            chat_id=chat_id,
            raw_text="second",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_expired_message_out_not_eligible(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    from datetime import UTC, datetime, timedelta

    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    supplier = seed_suppliers[0]
    db_session.add(
        MessageOut(
            request_id=request.id,
            supplier_id=supplier.id,
            tg_message_id=99,
            chat_id=supplier.telegram_id or 0,
            text="ask",
            kind=MessageKind.ask,
            send_status="sent",
            sent_at=datetime.now(UTC) - timedelta(hours=200),
        )
    )
    await db_session.flush()
    await handle_reply(
        db_session,
        _dm(supplier.telegram_id or 0, 10002, "117900"),
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(select(Quote).where(Quote.request_id == request.id))
    assert quote is None
