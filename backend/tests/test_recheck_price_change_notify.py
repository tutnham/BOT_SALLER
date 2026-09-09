"""Recheck reply price-change notification (TECH DOC §9.4 step 4)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote, QuoteSource, RequestStatus, Supplier
from app.services.request_service import create_request


def _supplier_reply_update(
    *,
    update_id: int,
    supplier_telegram_id: int,
    text: str,
    reply_to_text: str | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": supplier_telegram_id, "first_name": "Supplier"},
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "text": text,
    }
    if reply_to_text is not None:
        message["reply_to_message"] = {
            "message_id": update_id * 10 - 1,
            "text": reply_to_text,
        }
    return {"update_id": update_id, "message": message}


@pytest.mark.asyncio
async def test_recheck_changed_price_notifies_group(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=supplier.id,
            price_initial=Decimal("85000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    request.status = RequestStatus.needs_recheck
    await db_session.flush()
    mock_telegram.sent.clear()

    assert supplier.telegram_id is not None
    payload = _supplier_reply_update(
        update_id=53001,
        supplier_telegram_id=supplier.telegram_id,
        text="Сейчас 87000",
        reply_to_text=f"Уточните, пожалуйста, актуальна ли цена по заявке #{request.id}?",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == Decimal("87000")

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("изменилась" in txt and "85000" in txt and "87000" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_recheck_unchanged_price_no_change_spam(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=supplier.id,
            price_initial=Decimal("85000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    request.status = RequestStatus.needs_recheck
    await db_session.flush()
    mock_telegram.sent.clear()

    assert supplier.telegram_id is not None
    payload = _supplier_reply_update(
        update_id=53002,
        supplier_telegram_id=supplier.telegram_id,
        text="Цена та же 85000",
        reply_to_text=f"Уточните, пожалуйста, актуальна ли цена по заявке #{request.id}?",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("85000" in txt for txt in group_sends)
    assert not any("изменилась" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_recheck_low_confidence_no_silent_overwrite(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
    mock_llm,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=supplier.id,
            price_initial=Decimal("85000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    request.status = RequestStatus.needs_recheck
    await db_session.flush()
    mock_telegram.sent.clear()

    # Free-form text without regex price → LLM path with low confidence.
    mock_llm.parse_result = {
        "available": None,
        "qty": None,
        "price": 99000,
        "min_sale_price": None,
        "condition": None,
        "note": None,
        "confidence": 0.2,
    }

    assert supplier.telegram_id is not None
    payload = _supplier_reply_update(
        update_id=53003,
        supplier_telegram_id=supplier.telegram_id,
        text="ну примерно как обычно по рынку",
        reply_to_text=f"Уточните, пожалуйста, актуальна ли цена по заявке #{request.id}?",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == Decimal("85000")

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("неуверенное" in txt for txt in group_sends)
    assert not any("изменилась" in txt for txt in group_sends)
