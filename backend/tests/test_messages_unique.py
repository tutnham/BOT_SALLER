"""UNIQUE(chat_id, tg_message_id) on messages_out / messages_in (TECH DOC §13)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, MessageKind, MessageOut, Request, RequestStatus


@pytest.mark.asyncio
async def test_messages_out_unique_chat_message_id(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers,
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="test",
        status=RequestStatus.open,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    db_session.add(
        MessageOut(
            request_id=request.id,
            supplier_id=supplier.id,
            tg_message_id=555,
            chat_id=supplier.telegram_id or 0,
            text="first",
            kind=MessageKind.ask,
        )
    )
    await db_session.flush()

    db_session.add(
        MessageOut(
            request_id=request.id,
            supplier_id=supplier.id,
            tg_message_id=555,
            chat_id=supplier.telegram_id or 0,
            text="duplicate",
            kind=MessageKind.ask,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_messages_in_unique_chat_message_id(
    db_session: AsyncSession,
    seed_suppliers,
) -> None:
    supplier = seed_suppliers[0]
    db_session.add(
        MessageIn(
            request_id=None,
            supplier_id=supplier.id,
            tg_message_id=777,
            chat_id=supplier.telegram_id or 0,
            raw_text="first",
        )
    )
    await db_session.flush()

    db_session.add(
        MessageIn(
            request_id=None,
            supplier_id=supplier.id,
            tg_message_id=777,
            chat_id=supplier.telegram_id or 0,
            raw_text="duplicate",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
