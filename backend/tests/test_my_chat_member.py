"""Tests for my_chat_member events creating pending chats."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Owner, PendingChat, SupplierChat, SupplierChatType
from app.services.admin_service import add_supplier, set_supplier_default_chat
from app.telegram.keyboards import CallbackData
from tests.conftest import next_tg_update_id

OWNER_TG_ID = 300300300


def _my_chat_member_payload(
    chat_id: int,
    *,
    old_status: str = "left",
    new_status: str = "member",
    update_id: int = 100,
    inviter_id: int = OWNER_TG_ID,
) -> dict:
    return {
        "update_id": update_id,
        "my_chat_member": {
            "from": {"id": inviter_id},
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "old_chat_member": {"status": old_status},
            "new_chat_member": {"status": new_status, "user": {"id": 1, "is_bot": True}},
        },
    }


def _callback_payload(owner_id: int, data: CallbackData, *, chat_id: int = OWNER_TG_ID) -> dict:
    return {
        "update_id": next_tg_update_id(),
        "callback_query": {
            "id": "cb1",
            "from": {"id": owner_id},
            "message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"}},
            "data": data.encode(),
        },
    }


@pytest.mark.asyncio
async def test_bot_added_to_group_creates_pending_chat(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    chat_id = -1007777777777
    payload = _my_chat_member_payload(chat_id)
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_bind_pending_chat_as_supplier(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    chat_id = -1007777777777
    await webhook_client.post(
        "/telegram/webhook",
        json=_my_chat_member_payload(chat_id),
        headers=webhook_headers,
    )

    supplier = await add_supplier(db_session, name="Bound Supplier", telegram_id=2000)

    payload = _callback_payload(
        OWNER_TG_ID,
        CallbackData(namespace="admin", action="bind_supplier_to_chat", arg=chat_id, page=supplier.id),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    pending = await db_session.execute(select(PendingChat).where(PendingChat.chat_id == chat_id))
    assert pending.scalar_one_or_none() is None

    chat = await db_session.execute(
        select(SupplierChat).where(SupplierChat.chat_id == chat_id)
    )
    assert chat.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_bot_removed_from_group_deactivates_binding(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    chat_id = -1007777777777
    supplier = await add_supplier(db_session, name="Group Supplier", telegram_id=2001)
    db_session.add(
        SupplierChat(
            supplier_id=supplier.id,
            chat_id=chat_id,
            chat_type=SupplierChatType.supergroup,
            active=True,
            is_default=False,
        )
    )
    await db_session.flush()
    await set_supplier_default_chat(db_session, supplier.id, chat_id)

    payload = _my_chat_member_payload(chat_id, old_status="member", new_status="left")
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    chat = (
        await db_session.execute(select(SupplierChat).where(SupplierChat.chat_id == chat_id))
    ).scalar_one()
    await db_session.refresh(chat)
    assert chat.active is False
    assert chat.is_default is False
