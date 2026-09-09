"""Supplier reply → regex quote + structured group message (TECH DOC §9.2, §15.7)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote, Supplier
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
async def test_confident_reply_creates_quote_and_structured_message(
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
    await db_session.flush()
    mock_telegram.sent.clear()

    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    payload = _supplier_reply_update(
        update_id=40001,
        supplier_telegram_id=supplier.telegram_id,
        text="Есть, 85000 руб, кол-во 2",
        reply_to_text=f"Запрос #{request.id}\nmodel text",
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
    assert quote.price_initial == 85000
    assert quote.qty == 2
    assert quote.source.value == "regex"
    assert quote.confidence >= 0.75

    group_sends = [
        txt for cid, txt, *_ in mock_telegram.sent if cid == seed_group_chat_id
    ]
    assert len(group_sends) == 1
    assert "Наличие:" in group_sends[0]
    assert "Цена:" in group_sends[0]
    assert "Кол-во:" in group_sends[0]
    assert "85000" in group_sends[0]


@pytest.mark.asyncio
async def test_low_confidence_no_quote_low_confidence_template(
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
    await db_session.flush()
    mock_telegram.sent.clear()

    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    payload = _supplier_reply_update(
        update_id=40002,
        supplier_telegram_id=supplier.telegram_id,
        text="Перезвоните позже, обсудим",
        reply_to_text=f"Запрос #{request.id}\nmodel text",
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
    assert quote is None

    group_sends = [
        txt for cid, txt, *_ in mock_telegram.sent if cid == seed_group_chat_id
    ]
    assert len(group_sends) == 1
    assert "[распознавание неуверенное]" in group_sends[0]
    assert "Перезвоните позже" in group_sends[0]
