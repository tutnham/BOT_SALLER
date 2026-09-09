"""NL synonym «беру» maps to /deal with strict ambiguity rules."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, Quote, QuoteSource, RequestStatus, Supplier
from app.parsers.nl_commands import parse_nl_command
from app.services.request_service import create_request


def _group_nl_update(
    *,
    update_id: int,
    from_id: int,
    chat_id: int,
    text: str,
    reply_to_bot: bool = False,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": from_id, "first_name": "Employee"},
        "chat": {"id": chat_id, "type": "supergroup"},
        "text": text,
    }
    if reply_to_bot:
        message["reply_to_message"] = {
            "message_id": update_id * 10 - 1,
            "from": {"id": 1, "is_bot": True, "first_name": "Bot"},
            "text": "status",
        }
    return {"update_id": update_id, "message": message}


def test_parse_nl_deal_extracts_args() -> None:
    parsed = parse_nl_command(
        "беру #12 у поставщика 3 за 85000",
    )
    assert parsed is not None
    assert parsed.intent == "deal"
    assert parsed.request_id == 12
    assert parsed.supplier_id == 3
    assert parsed.price == 85000.0


@pytest.mark.asyncio
async def test_nl_deal_without_gate_ignored(
    webhook_client: AsyncClient,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    mock_telegram.sent.clear()
    payload = _group_nl_update(
        update_id=56001,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text="беру #1 у поставщика 2 за 85000",
        reply_to_bot=False,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert mock_telegram.sent == []


@pytest.mark.asyncio
async def test_nl_deal_incomplete_args_clarifies(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
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

    payload = _group_nl_update(
        update_id=56002,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text=f"беру #{request.id}",
        reply_to_bot=True,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("Укажите заявку, поставщика и цену" in txt for txt in group_sends)

    deals = await db_session.scalars(select(Deal))
    assert list(deals) == []


@pytest.mark.asyncio
async def test_nl_deal_complete_args_closes_request(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
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
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=seed_suppliers[0].id,
            price_initial=Decimal("85000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    request.status = RequestStatus.priced
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _group_nl_update(
        update_id=56003,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text=(
            f"беру #{request.id} у поставщика {seed_suppliers[0].id} "
            "за 84000"
        ),
        reply_to_bot=True,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    await db_session.refresh(request)
    assert request.status == RequestStatus.closed

    deal = await db_session.scalar(
        select(Deal).where(Deal.request_id == request.id)
    )
    assert deal is not None
    assert deal.chosen_supplier_id == seed_suppliers[0].id


@pytest.mark.asyncio
async def test_nl_deal_non_employee_ignored(
    webhook_client: AsyncClient,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    mock_telegram.sent.clear()
    payload = _group_nl_update(
        update_id=56004,
        from_id=888888888,
        chat_id=seed_group_chat_id,
        text="беру #1 у поставщика 2 за 85000",
        reply_to_bot=True,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert mock_telegram.sent == []
