"""Webhook tests for /bargain (TECH DOC §9.3, Phase 5)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.db.models import (
    MessageKind,
    MessageOut,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    Supplier,
)
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


async def _priced_request(
    db_session: AsyncSession,
    *,
    seed_employee,
    seed_group_chat_id: int,
    mock_telegram,
    seed_suppliers: list[Supplier],
) -> tuple[Request, Supplier]:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 256GB",
        telegram=mock_telegram,
    )
    supplier_a = seed_suppliers[0]
    supplier_b = seed_suppliers[1]
    db_session.add_all(
        [
            Quote(
                request_id=request.id,
                supplier_id=supplier_a.id,
                price_initial=Decimal("85000"),
                source=QuoteSource.manual,
                confidence=1.0,
            ),
            Quote(
                request_id=request.id,
                supplier_id=supplier_b.id,
                price_initial=Decimal("90000"),
                source=QuoteSource.manual,
                confidence=1.0,
            ),
        ]
    )
    request.status = RequestStatus.priced
    await db_session.flush()
    return request, supplier_a


@pytest.mark.asyncio
async def test_bargain_non_employee_ignored(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request, _ = await _priced_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
    )
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=50001,
        employee_telegram_id=999999,
        group_chat_id=seed_group_chat_id,
        text=f"/bargain {request.id} 80000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"

    outs = await db_session.scalars(
        select(MessageOut).where(MessageOut.kind == MessageKind.bargain)
    )
    assert list(outs) == []


@pytest.mark.asyncio
async def test_bargain_happy_path_sends_template_and_sets_status(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request, best_supplier = await _priced_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
    )
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=50002,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/bargain {request.id} 80000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    await db_session.refresh(request)
    assert request.status == RequestStatus.bargaining

    bargain_out = await db_session.scalar(
        select(MessageOut).where(
            MessageOut.request_id == request.id,
            MessageOut.kind == MessageKind.bargain,
        )
    )
    assert bargain_out is not None
    assert bargain_out.supplier_id == best_supplier.id
    assert "80000" in bargain_out.text
    assert f"#{request.id}" in bargain_out.text
    # No competitor prices in outbound supplier text.
    assert "90000" not in bargain_out.text
    assert "85000" not in bargain_out.text

    supplier_sends = [
        txt for cid, txt in mock_telegram.sent if cid == best_supplier.telegram_id
    ]
    assert len(supplier_sends) == 1
    assert "80000" in supplier_sends[0]
    assert "90000" not in supplier_sends[0]

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any(f"(#{best_supplier.id})" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_bargain_missing_request(
    webhook_client: AsyncClient,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    mock_telegram.sent.clear()
    payload = _employee_command_update(
        update_id=50003,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text="/bargain 999999 80000",
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
async def test_bargain_no_quotes(
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
    request.status = RequestStatus.awaiting_answers
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=50004,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/bargain {request.id} 80000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("нет предложений" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_bargain_optional_supplier_id(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request, _ = await _priced_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
    )
    chosen = seed_suppliers[1]
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=50005,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/bargain {request.id} 81000 {chosen.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    bargain_out = await db_session.scalar(
        select(MessageOut).where(
            MessageOut.request_id == request.id,
            MessageOut.kind == MessageKind.bargain,
        )
    )
    assert bargain_out is not None
    assert bargain_out.supplier_id == chosen.id
