"""Webhook supplier reply — raw forward (TECH DOC §9.2, §15 п.2)."""

from __future__ import annotations

import pytest
from app.db.models import MessageIn, MessageOut, RequestStatus, Supplier
from app.services.request_service import create_request
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


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
async def test_supplier_reply_with_hash_n_forwards_to_group(
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
        update_id=20001,
        supplier_telegram_id=supplier.telegram_id,
        text="Есть, 85000 руб",
        reply_to_text=f"Запрос #{request.id}\nmodel text",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    msg_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == payload["message"]["message_id"])
    )
    assert msg_in is not None
    assert msg_in.request_id == request.id
    assert msg_in.raw_text == "Есть, 85000 руб"

    group_sends = [
        txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id
    ]
    assert len(group_sends) == 1
    assert supplier.name in group_sends[0]
    assert f"Заявка #{request.id}" in group_sends[0]
    assert "85000" in group_sends[0]


@pytest.mark.asyncio
async def test_unbound_supplier_reply_saves_null_request_and_prompts(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    payload = _supplier_reply_update(
        update_id=20002,
        supplier_telegram_id=supplier.telegram_id,
        text="Случайный ответ без привязки",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    msg_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == payload["message"]["message_id"])
    )
    assert msg_in is not None
    assert msg_in.request_id is None

    dm_sends = [
        txt for cid, txt in mock_telegram.sent if cid == supplier.telegram_id
    ]
    assert len(dm_sends) == 1
    assert "#N" in dm_sends[0]


@pytest.mark.asyncio
async def test_supplier_fallback_to_last_active_request(
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
        source_text="Samsung S25",
        telegram=mock_telegram,
    )
    request.status = RequestStatus.awaiting_answers
    await db_session.flush()
    mock_telegram.sent.clear()

    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    payload = _supplier_reply_update(
        update_id=20003,
        supplier_telegram_id=supplier.telegram_id,
        text="В наличии 70000",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    msg_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == payload["message"]["message_id"])
    )
    assert msg_in is not None
    assert msg_in.request_id == request.id

    out_exists = await db_session.scalar(
        select(MessageOut.id).where(
            MessageOut.request_id == request.id,
            MessageOut.supplier_id == supplier.id,
        )
    )
    assert out_exists is not None


@pytest.mark.asyncio
async def test_supplier_cannot_bind_foreign_request_id(
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
        source_text="iPhone 16 Pro",
        telegram=mock_telegram,
    )
    await db_session.flush()
    mock_telegram.sent.clear()

    foreign_supplier = seed_suppliers[3]
    assert foreign_supplier.telegram_id is not None

    payload = _supplier_reply_update(
        update_id=20004,
        supplier_telegram_id=foreign_supplier.telegram_id,
        text="Есть, 91000",
        reply_to_text=f"Запрос #{request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    msg_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == payload["message"]["message_id"])
    )
    assert msg_in is not None
    assert msg_in.request_id is None
    assert any(cid == foreign_supplier.telegram_id for cid, _ in mock_telegram.sent)
