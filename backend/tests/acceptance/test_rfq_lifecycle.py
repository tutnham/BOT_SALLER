"""RFQ acceptance scenarios. Real Postgres, fake Telegram and LLM."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Deal,
    MessageIn,
    ProductCategory,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    SupplierCategory,
)
from app.handlers.supplier_messages import handle_reply, process_bound_supplier_reply
from app.services.deal_service import (
    RequestNotOpenError,
    SupplierNotFoundError,
    create_deal,
)
from app.services.inbox_service import enqueue_webhook_update, process_inbox_batch
from app.services.quote_service import upsert_quote
from app.services.request_service import create_request, load_request_for_employee
from app.templates.messages_ru import render_template
from tests.conftest import MockTelegramClient

pytestmark = pytest.mark.acceptance


def _dm(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


@pytest.mark.asyncio
async def test_ask_creates_request_for_matching_category_only(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    kept = seed_suppliers[0]
    for supplier in seed_suppliers[1:]:
        supplier.active = False
    await db_session.execute(
        delete(SupplierCategory).where(SupplierCategory.supplier_id == kept.id)
    )
    db_session.add(
        SupplierCategory(supplier_id=kept.id, category=ProductCategory.apple.value)
    )
    await db_session.flush()

    outcome = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    assert outcome.requests
    supplier_ids = {
        chat_id
        for chat_id, _text, _markup in mock_telegram.sent
        if chat_id == kept.telegram_id
    }
    assert supplier_ids == {kept.telegram_id}
    assert all(item[0] != seed_suppliers[1].telegram_id for item in mock_telegram.sent)


@pytest.mark.asyncio
async def test_replay_same_update_id_does_not_enqueue_twice(
    db_session: AsyncSession,
) -> None:
    payload = {"update_id": 960001}
    first, _ = await enqueue_webhook_update(
        db_session, tg_update_id=960001, payload=payload
    )
    second, _ = await enqueue_webhook_update(
        db_session, tg_update_id=960001, payload=payload
    )
    assert first == "queued"
    assert second == "duplicate"


@pytest.mark.asyncio
async def test_bare_price_with_two_requests_does_not_create_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    for _ in range(2):
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
        _dm(supplier.telegram_id, 9601, "90000"),
        telegram=mock_telegram,
    )
    count = await db_session.scalar(select(func.count()).select_from(Quote))
    assert count == 0
    assert any("К какой заявке" in text for _chat, text, _markup in mock_telegram.sent)


@pytest.mark.asyncio
async def test_low_confidence_does_not_create_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    outcome = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    quote = await upsert_quote(
        db_session,
        outcome.requests[0].id,
        seed_suppliers[0].id,
        price=Decimal("100"),
        source=QuoteSource.llm,
        confidence=0.2,
    )
    assert quote is None


@pytest.mark.asyncio
async def test_bargain_template_has_no_competitor_price() -> None:
    text = render_template("bargain", request_id=7, target_price="1000")
    assert "1000" in text
    assert "конкурент" not in text.lower()


@pytest.mark.asyncio
async def test_deal_closes_request_once(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
) -> None:
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    deal = await create_deal(
        db_session,
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        final_price=Decimal("1500.00"),
    )
    assert deal.id is not None
    assert request.status == RequestStatus.closed
    with pytest.raises(RequestNotOpenError):
        await create_deal(
            db_session,
            request_id=request.id,
            supplier_id=seed_suppliers[0].id,
            final_price=Decimal("1500.00"),
        )
    deals = await db_session.scalar(
        select(func.count()).select_from(Deal).where(Deal.request_id == request.id)
    )
    assert deals == 1


@pytest.mark.asyncio
async def test_closed_request_reply_does_not_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.closed,
    )
    db_session.add(request)
    await db_session.flush()
    message_in = MessageIn(
        supplier_id=seed_suppliers[0].id,
        chat_id=seed_suppliers[0].telegram_id or 1,
        tg_message_id=9602,
        raw_text="1000",
    )
    db_session.add(message_in)
    await db_session.flush()
    quote = await process_bound_supplier_reply(
        db_session,
        request=request,
        supplier=seed_suppliers[0],
        raw_text="1000",
        message_in=message_in,
        telegram=mock_telegram,
        business_connection_id=None,
        chat_id=int(seed_suppliers[0].telegram_id or 1),
        bind_method="reply",
    )
    assert quote is None


@pytest.mark.asyncio
async def test_employee_cannot_load_other_group_request(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
) -> None:
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.open,
    )
    db_session.add(request)
    await db_session.flush()
    hidden = await load_request_for_employee(
        db_session,
        request_id=request.id,
        employee_id=seed_employee.id,
        chat_id=seed_client_group.chat_id - 1,
        is_private_chat=False,
    )
    assert hidden is None


@pytest.mark.asyncio
async def test_inbox_survives_handler_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.telegram import webhook_dispatcher

    async def _boom(_session, _payload):
        raise RuntimeError("llm down")

    monkeypatch.setattr(webhook_dispatcher, "process_telegram_update", _boom)
    status, _inbox_id = await enqueue_webhook_update(
        db_session,
        tg_update_id=960003,
        payload={"update_id": 960003},
    )
    assert status == "queued"
    await db_session.commit()
    processed = await process_inbox_batch(db_session, worker_id="acceptance")
    assert processed == 0


@pytest.mark.asyncio
async def test_inactive_supplier_is_not_a_deal_target(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
) -> None:
    inactive = seed_suppliers[2]
    assert inactive.active is False
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    with pytest.raises(SupplierNotFoundError):
        await create_deal(
            db_session,
            request_id=request.id,
            supplier_id=inactive.id,
            final_price=Decimal("10"),
        )
