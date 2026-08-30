"""Tests for owner admin menu via inline callbacks."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminDialog, Owner, Supplier, SupplierChat, SupplierChatType
from sqlalchemy import select
from app.db.session import get_db
from app.main import app
from app.telegram.client import set_telegram_client
from app.telegram.keyboards import CallbackData, menu_button

OWNER_TG_ID = 300300300


def _callback_payload(
    owner_id: int,
    callback_id: str,
    data: CallbackData,
    *,
    chat_id: int = OWNER_TG_ID,
    message_id: int = 1,
) -> dict:
    return {
        "update_id": 100,
        "callback_query": {
            "id": callback_id,
            "from": {"id": owner_id},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data.encode(),
        },
    }


def _menu_command_payload(owner_id: int, text: str = "/menu") -> dict:
    return {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "from": {"id": owner_id},
            "chat": {"id": owner_id, "type": "private"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_menu_callback_requires_owner(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
) -> None:
    payload = _callback_payload(999, "cb1", CallbackData(namespace="admin", action="main_menu"))
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_add_supplier_dialog(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    # Open menu
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_menu_command_payload(OWNER_TG_ID),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    # Click add supplier
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb2",
        CallbackData(namespace="admin", action="supplier_add_start"),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is not None
    assert dialog.state == "await_supplier_name"

    # Send name
    text_payload = {
        "update_id": 101,
        "message": {
            "message_id": 2,
            "from": {"id": OWNER_TG_ID},
            "chat": {"id": OWNER_TG_ID, "type": "private"},
            "text": "New Supplier",
        },
    }
    resp = await webhook_client.post(
        "/telegram/webhook", json=text_payload, headers=webhook_headers
    )
    assert resp.json()["status"] == "ok"

    supplier = (
        await db_session.execute(
            select(Supplier).where(Supplier.name == "New Supplier")
        )
    ).scalar_one_or_none()
    assert supplier is not None
    assert supplier.active is True

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is None


@pytest.mark.asyncio
async def test_toggle_supplier_rfq(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    seed_suppliers: list[Supplier],
) -> None:
    supplier = seed_suppliers[0]
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb",
        CallbackData(namespace="admin", action="supplier_toggle_rfq", arg=supplier.id),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.rfq_enabled is False


@pytest.mark.asyncio
async def test_set_default_chat(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    seed_suppliers: list[Supplier],
) -> None:
    supplier = seed_suppliers[0]
    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-100500,
        chat_type=SupplierChatType.supergroup,
        active=True,
    )
    db_session.add(chat)
    await db_session.flush()

    payload = _callback_payload(
        OWNER_TG_ID,
        "cb",
        CallbackData(
            namespace="admin",
            action="supplier_set_default",
            arg=supplier.id,
            page=chat.chat_id,
        ),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    await db_session.refresh(chat)
    assert chat.is_default is True


@pytest.mark.asyncio
async def test_idempotent_callback(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb",
        CallbackData(namespace="admin", action="main_menu"),
    )
    resp1 = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    resp2 = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp1.json()["status"] == "ok"
    assert resp2.json()["status"] == "duplicate"
