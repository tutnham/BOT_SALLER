"""Manual bind must reject closed/cancelled requests."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.manual_bind import manual_bind_message
from app.db.models import MessageIn, RequestStatus
from app.handlers.supplier_messages import handle_reply
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient


@pytest.mark.asyncio
async def test_manual_bind_closed_request_rejected(
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
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    await handle_reply(
        db_session,
        {
            "message_id": 9901,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "117900",
        },
        telegram=mock_telegram,
    )
    from sqlalchemy import select

    message_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 9901)
    )
    assert message_in is not None

    request.status = RequestStatus.closed
    await db_session.flush()

    result = await manual_bind_message(
        db_session,
        message_in_id=message_in.id,
        request_id=request.id,
        telegram=mock_telegram,
    )
    assert result == "request_not_open"
