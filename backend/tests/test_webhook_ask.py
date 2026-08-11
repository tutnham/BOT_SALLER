"""Webhook /ask flow (TECH DOC §9.1, §15 п.1,3)."""

from __future__ import annotations

import pytest
from app.db.models import MessageOut, Request, RequestStatus
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


def _group_ask_update(
    *,
    update_id: int,
    from_id: int,
    chat_id: int,
    text: str = "/ask iPhone 17 256GB",
    reply_text: str | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": from_id, "first_name": "Employee"},
        "chat": {"id": chat_id, "type": "supergroup"},
        "text": text,
    }
    if reply_text is not None:
        message["reply_to_message"] = {
            "message_id": update_id * 10 - 1,
            "text": reply_text,
        }
    return {"update_id": update_id, "message": message}


@pytest.mark.asyncio
async def test_non_employee_ask_ignored_no_messages_out(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_group_chat_id: int,
) -> None:
    payload = _group_ask_update(
        update_id=10001,
        from_id=999999,
        chat_id=seed_group_chat_id,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"

    count = await db_session.scalar(select(func.count()).select_from(MessageOut))
    assert count == 0


@pytest.mark.asyncio
async def test_employee_ask_creates_request_and_messages_out(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_supplier_count: int,
    mock_telegram,
) -> None:
    payload = _group_ask_update(
        update_id=10002,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text="/ask",
        reply_text="Нужен iPhone 17 Pro 256",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    request = await db_session.scalar(select(Request).limit(1))
    assert request is not None
    assert request.status == RequestStatus.awaiting_answers
    assert "iPhone 17 Pro 256" in request.source_text

    out_count = await db_session.scalar(select(func.count()).select_from(MessageOut))
    assert out_count == seed_supplier_count

    supplier_sends = [
        (cid, txt) for cid, txt in mock_telegram.sent if cid != seed_group_chat_id
    ]
    assert len(supplier_sends) == seed_supplier_count
    assert all(f"Запрос #{request.id}" in txt for _, txt in supplier_sends)


@pytest.mark.asyncio
async def test_duplicate_update_id_no_second_broadcast(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_supplier_count: int,
) -> None:
    payload = _group_ask_update(
        update_id=10003,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
    )
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"}

    first = await webhook_client.post("/telegram/webhook", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "ok"

    out_after_first = await db_session.scalar(
        select(func.count()).select_from(MessageOut)
    )

    second = await webhook_client.post("/telegram/webhook", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    out_after_second = await db_session.scalar(
        select(func.count()).select_from(MessageOut)
    )
    assert out_after_second == out_after_first == seed_supplier_count

    req_count = await db_session.scalar(select(func.count()).select_from(Request))
    assert req_count == 1
