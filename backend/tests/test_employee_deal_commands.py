"""Webhook tests for /setprice, /deal, /status, /cancel (TECH DOC §9.3.1, §11)."""

from __future__ import annotations

import pytest
from app.db.models import Deal, Quote, Request, RequestStatus, Supplier
from app.services.request_service import create_request
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _employee_command_update(
    *,
    update_id: int,
    employee_telegram_id: int,
    group_chat_id: int,
    text: str,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": employee_telegram_id, "first_name": "Employee"},
            "chat": {"id": group_chat_id, "type": "supergroup"},
            "text": text,
        },
    }


async def _create_test_request(
    db_session: AsyncSession,
    *,
    seed_employee,
    seed_group_chat_id: int,
    mock_telegram,
) -> Request:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 256GB",
        telegram=mock_telegram,
    )
    await db_session.flush()
    return request


@pytest.mark.asyncio
async def test_setprice_creates_manual_quote(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request = await _create_test_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=30001,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/setprice {request.id} {supplier.id} 82000 2",
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
    assert quote.price_initial == 82000
    assert quote.qty == 2
    assert quote.source.value == "manual"

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("зафиксирована" in txt for txt in group_sends)
    assert any(f"(#{supplier.id})" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_setprice_invalid_command(
    webhook_client: AsyncClient,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    mock_telegram.sent.clear()
    payload = _employee_command_update(
        update_id=30002,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text="/setprice bad args",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("Неверный формат" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_deal_closes_request(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request = await _create_test_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=30003,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/deal {request.id} {supplier.id} 81000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    await db_session.refresh(request)
    assert request.status == RequestStatus.closed

    deal = await db_session.scalar(
        select(Deal).where(Deal.request_id == request.id)
    )
    assert deal is not None
    assert deal.final_price == 81000
    assert deal.chosen_supplier_id == supplier.id

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("закрыта" in txt for txt in group_sends)
    assert any(f"(#{supplier.id})" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_deal_rejects_second_deal(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request = await _create_test_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    request.status = RequestStatus.closed
    db_session.add(
        Deal(
            request_id=request.id,
            chosen_supplier_id=supplier.id,
            final_price=80000,
        )
    )
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=30004,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/deal {request.id} {supplier.id} 79000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("уже есть сделка" in txt or "закрыта" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_deal_request_not_found(
    webhook_client: AsyncClient,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    supplier = seed_suppliers[0]
    mock_telegram.sent.clear()
    payload = _employee_command_update(
        update_id=30005,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/deal 99999 {supplier.id} 80000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("не найдена" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_status_shows_quotes_and_deal(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request = await _create_test_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=supplier.id,
            price_initial=85000,
            qty=1,
            available=True,
            source="manual",
            confidence=1.0,
        )
    )
    db_session.add(
        Deal(
            request_id=request.id,
            chosen_supplier_id=supplier.id,
            final_price=84000,
        )
    )
    request.status = RequestStatus.closed
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=30006,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/status {request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert len(group_sends) == 1
    summary = group_sends[0]
    assert f"Заявка #{request.id}" in summary
    assert supplier.name in summary
    assert f"(#{supplier.id})" in summary
    assert "85000" in summary
    assert "Сделка" in summary


@pytest.mark.asyncio
async def test_cancel_request(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    request = await _create_test_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
    )
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=30007,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/cancel {request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    await db_session.refresh(request)
    assert request.status == RequestStatus.cancelled

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("отменена" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_cancel_on_closed_rejects(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    request = await _create_test_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
    )
    request.status = RequestStatus.closed
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=30008,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/cancel {request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    await db_session.refresh(request)
    assert request.status == RequestStatus.closed

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("закрыта или отменена" in txt for txt in group_sends)
