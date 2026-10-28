"""Request status card: counters, price ordering, and callback authorization."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote, QuoteSource, Request, RequestStatus, Supplier
from app.handlers.request_status_callbacks import handle_request_status_callback
from app.services.request_card_service import render_all_prices, render_request_card
from app.telegram.keyboards import CallbackData
from tests.conftest import MockTelegramClient


async def _request(session: AsyncSession, employee_id: int) -> Request:
    request = Request(
        group_chat_id=-1001,
        employee_id=employee_id,
        source_text="iPhone 17 Air 256GB Black",
        status=RequestStatus.awaiting_answers,
        requested_qty=1,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    session.add(request)
    await session.flush()
    return request


@pytest.mark.asyncio
async def test_card_shows_counters_and_prices(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    request = await _request(db_session, seed_employee.id)
    db_session.add(
        Quote(
            request_id=request.id, supplier_id=seed_suppliers[0].id,
            price_initial=Decimal("91000"), source=QuoteSource.regex,
            confidence=0.9, available=True, price_final=Decimal("91500"),
        )
    )
    await db_session.flush()
    card = await render_request_card(db_session, request)
    assert "с ценой: 1" in card
    assert "91000" in card


@pytest.mark.asyncio
async def test_all_prices_sorted_ascending(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier]
) -> None:
    request = await _request(db_session, seed_employee.id)
    for supplier, price in (
        (seed_suppliers[0], Decimal("99000")),
        (seed_suppliers[1], Decimal("90000")),
    ):
        db_session.add(
            Quote(
                request_id=request.id, supplier_id=supplier.id, price_initial=price,
                source=QuoteSource.regex, confidence=0.9, available=True,
            )
        )
    await db_session.flush()
    text = await render_all_prices(db_session, request.id, page=0)
    assert text.index("90000") < text.index("99000")


@pytest.mark.asyncio
async def test_unauthorized_callback_rejected(db_session: AsyncSession) -> None:
    telegram = MockTelegramClient()
    data = CallbackData(namespace="reqstatus", action="refresh", arg=1).encode()
    result = await handle_request_status_callback(
        db_session,
        {"id": "cb1", "from": {"id": 999999}, "data": data, "message": {"chat": {"id": 1}}},
        telegram=telegram,
    )
    assert result == "ignored"
    assert telegram.answer_callbacks[0][1] == "Нет доступа"
