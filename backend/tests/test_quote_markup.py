"""Quote markup persistence and group display price."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule, QuoteSource
from app.handlers.supplier_messages import handle_reply
from app.services.quote_service import display_price_for_group, upsert_quote
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient


@pytest.mark.asyncio
async def test_group_sees_final_price_not_supplier(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    seed_markup_rules,
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
            "message_id": 601,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "80000",
        },
        telegram=mock_telegram,
    )
    await handle_reply(
        db_session,
        {
            "message_id": 602,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "да",
        },
        telegram=mock_telegram,
    )
    group_msgs = [text for chat_id, text, _ in mock_telegram.sent if chat_id == seed_client_group.chat_id]
    assert any("80800" in text for text in group_msgs)
    assert all("80000" not in text for text in group_msgs)


@pytest.mark.asyncio
async def test_upsert_quote_sets_price_final(
    db_session: AsyncSession,
    seed_markup_rules: list[MarkupRule],
) -> None:
    from app.db.models import Employee, Request, RequestStatus

    employee = Employee(telegram_id=999001, name="E", active=True)
    db_session.add(employee)
    await db_session.flush()
    request = Request(
        group_chat_id=-100999,
        employee_id=employee.id,
        source_text="iPhone 17 Air",
        normalized_json={"model": "iPhone 17 Air", "category": "apple"},
        status=RequestStatus.open,
    )
    db_session.add(request)
    await db_session.flush()
    from app.db.models import Supplier

    supplier = Supplier(name="S", telegram_id=888001, active=True, dm_ok=True)
    db_session.add(supplier)
    await db_session.flush()
    quote = await upsert_quote(
        db_session,
        request.id,
        supplier_id=supplier.id,
        price=70000,
        source=QuoteSource.manual,
    )
    assert quote is not None
    assert quote.price_final == 70500
    assert display_price_for_group(quote) == 70500
