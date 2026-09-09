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
    reply_to_text: str,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": supplier_telegram_id, "first_name": "Supplier"},
            "chat": {"id": supplier_telegram_id, "type": "private"},
            "text": text,
            "reply_to_message": {"message_id": update_id * 10 - 1, "text": reply_to_text},
        },
    }


@pytest.mark.asyncio
async def test_ambiguous_reply_uses_llm_and_writes_llm_source(
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
    await db_session.flush()
    mock_telegram.sent.clear()
    mock_llm.parse_calls = 0
    mock_llm.parse_result = {
        "available": True,
        "qty": 3,
        "price": 87000,
        "min_sale_price": None,
        "condition": None,
        "note": "LLM",
        "confidence": 0.92,
    }

    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    payload = _supplier_reply_update(
        update_id=51001,
        supplier_telegram_id=supplier.telegram_id,
        text="Уточню позже, но да",
        reply_to_text=f"Запрос #{request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert mock_llm.parse_calls == 1

    quote = await db_session.scalar(
        select(Quote).where(Quote.request_id == request.id, Quote.supplier_id == supplier.id)
    )
    assert quote is not None
    assert quote.source.value == "llm"
    assert float(quote.price_initial) == 87000


@pytest.mark.asyncio
async def test_same_text_hits_cache_and_skips_second_llm_call(
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
        source_text="Samsung S25",
        telegram=mock_telegram,
    )
    await db_session.flush()
    mock_telegram.sent.clear()
    mock_llm.parse_calls = 0
    mock_llm.parse_result = {
        "available": True,
        "qty": 1,
        "price": 71000,
        "min_sale_price": None,
        "condition": None,
        "note": None,
        "confidence": 0.9,
    }

    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    text = "Сейчас точных цифр нет, но подтверждаю"

    first = _supplier_reply_update(
        update_id=51002,
        supplier_telegram_id=supplier.telegram_id,
        text=text,
        reply_to_text=f"Запрос #{request.id}",
    )
    second = _supplier_reply_update(
        update_id=51003,
        supplier_telegram_id=supplier.telegram_id,
        text=text,
        reply_to_text=f"Запрос #{request.id}",
    )
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"}
    assert (await webhook_client.post("/telegram/webhook", json=first, headers=headers)).status_code == 200
    assert (await webhook_client.post("/telegram/webhook", json=second, headers=headers)).status_code == 200
    assert mock_llm.parse_calls == 1


@pytest.mark.asyncio
async def test_regex_price_reply_skips_llm(
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
        source_text="Pixel 9",
        telegram=mock_telegram,
    )
    await db_session.flush()
    mock_telegram.sent.clear()
    mock_llm.parse_calls = 0

    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    payload = _supplier_reply_update(
        update_id=51004,
        supplier_telegram_id=supplier.telegram_id,
        text="Есть, 65000 руб, кол-во 2",
        reply_to_text=f"Запрос #{request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert mock_llm.parse_calls == 0
