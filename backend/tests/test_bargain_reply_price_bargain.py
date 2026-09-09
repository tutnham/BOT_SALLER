"""Supplier bargain reply writes price_bargain without auto-deal (TECH DOC §9.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Deal,
    Quote,
    QuoteSource,
    RequestStatus,
    Supplier,
)
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
async def test_bargain_reply_sets_price_bargain_no_deal(
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
    request.status = RequestStatus.bargaining
    await db_session.flush()
    mock_telegram.sent.clear()

    assert supplier.telegram_id is not None
    payload = _supplier_reply_update(
        update_id=51001,
        supplier_telegram_id=supplier.telegram_id,
        text="Могу 80000",
        reply_to_text=f"По заявке #{request.id} — есть возможность сделать цену 80000 ₽?",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == Decimal("85000")
    assert quote.price_bargain == Decimal("80000")

    await db_session.refresh(request)
    assert request.status == RequestStatus.bargaining

    deal_count = await db_session.scalar(select(func.count()).select_from(Deal))
    assert deal_count == 0
