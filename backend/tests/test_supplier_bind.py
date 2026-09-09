"""Tests for owner self-service supplier Telegram binding."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Owner,
    Supplier,
    SupplierChat,
    SupplierChatType,
)
from app.services import admin_service
from app.telegram.keyboards import CallbackData
from tests.conftest import next_tg_update_id

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
        "update_id": next_tg_update_id(),
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


def _owner_text_payload(
    owner_id: int,
    *,
    update_id: int,
    text: str = "",
    forward_from: dict | None = None,
    forward_origin: dict | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": owner_id},
        "chat": {"id": owner_id, "type": "private"},
        "text": text,
    }
    if forward_from is not None:
        message["forward_from"] = forward_from
    if forward_origin is not None:
        message["forward_origin"] = forward_origin
    return {"update_id": update_id, "message": message}


def _private_update(
    update_id: int,
    from_id: int,
    text: str,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": from_id, "first_name": "User"},
            "chat": {"id": from_id, "type": "private"},
            "text": text,
        },
    }


def _callback_data_from_markup(markup: dict | None) -> list[str]:
    if markup is None:
        return []
    rows = markup.get("inline_keyboard") or []
    result: list[str] = []
    for row in rows:
        for btn in row:
            data = btn.get("callback_data")
            if data:
                result.append(data)
    return result


def _find_button(markup: dict | None, substring: str) -> str | None:
    if markup is None:
        return None
    rows = markup.get("inline_keyboard") or []
    for row in rows:
        for btn in row:
            if substring in (btn.get("text") or ""):
                return btn.get("callback_data")
    return None


@pytest.mark.asyncio
async def test_add_supplier_skip_no_telegram_id(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    # Start add supplier
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_add_sup",
            CallbackData(namespace="admin", action="supplier_add_start"),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    # Type name
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=101, text="Ромашка"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    supplier = (
        await db_session.execute(
            select(Supplier).where(Supplier.name == "Ромашка")
        )
    ).scalar_one_or_none()
    assert supplier is not None
    assert supplier.telegram_id is None
    assert supplier.dm_ok is False

    # Click skip directly
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_skip",
            CallbackData(namespace="admin", action="supplier_bind_skip", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    from app.db.models import AdminDialog
    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is None


@pytest.mark.asyncio
async def test_add_supplier_forward_sets_telegram_id(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_add_sup",
            CallbackData(namespace="admin", action="supplier_add_start"),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=201, text="Ромашка"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    supplier = (
        await db_session.execute(
            select(Supplier).where(Supplier.name == "Ромашка")
        )
    ).scalar_one_or_none()
    assert supplier is not None
    supplier_id = supplier.id

    # Click forward button
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_fwd",
            CallbackData(namespace="admin", action="supplier_bind_forward", arg=supplier_id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    # Forward a message from supplier
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(
            OWNER_TG_ID,
            update_id=202,
            forward_from={"id": 900900900, "is_bot": False},
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.telegram_id == 900900900
    assert supplier.dm_ok is True

    chats = (
        await db_session.execute(
            select(SupplierChat).where(
                SupplierChat.supplier_id == supplier_id,
                SupplierChat.chat_type == SupplierChatType.private,
            )
        )
    ).scalars().all()
    assert len(chats) == 1
    assert chats[0].chat_id == 900900900
    assert chats[0].active is True


@pytest.mark.asyncio
async def test_add_supplier_forward_changes_telegram_id(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    supplier = Supplier(name="Ромашка", telegram_id=900900900, active=True, dm_ok=True)
    db_session.add(supplier)
    await db_session.flush()

    # Start binding flow for existing supplier
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_bind_start",
            CallbackData(namespace="admin", action="supplier_bind_start", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_fwd",
            CallbackData(namespace="admin", action="supplier_bind_forward", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(
            OWNER_TG_ID,
            update_id=301,
            forward_origin={
                "type": "user",
                "sender_user": {"id": 800800800, "is_bot": False},
            },
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.telegram_id == 800800800


@pytest.mark.asyncio
async def test_bind_link_start_token_dm_ok(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "zakupki_bot")

    supplier = Supplier(name="Ромашка", active=True, dm_ok=False)
    db_session.add(supplier)
    await db_session.flush()

    bind_token = await admin_service.create_supplier_bind_token(
        db_session, supplier_id=supplier.id, owner_id=OWNER_TG_ID
    )

    # Supplier opens deep link
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(
            update_id=401,
            from_id=900900900,
            text=f"/start {bind_token.token}",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.telegram_id == 900900900
    assert supplier.dm_ok is True

    await db_session.refresh(bind_token)
    assert bind_token.used_at is not None

    chats = (
        await db_session.execute(
            select(SupplierChat).where(SupplierChat.supplier_id == supplier.id)
        )
    ).scalars().all()
    assert any(c.chat_id == 900900900 for c in chats)

    # Second use of the same token should fail
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(
            update_id=402,
            from_id=800800800,
            text=f"/start {bind_token.token}",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    second_supplier = (
        await db_session.execute(
            select(Supplier).where(Supplier.telegram_id == 800800800)
        )
    ).scalar_one_or_none()
    assert second_supplier is None


@pytest.mark.asyncio
async def test_bind_link_conflict_burns_token_and_notifies_owner(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    seed_employee,  # type: ignore
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "zakupki_bot")

    supplier = Supplier(name="Ромашка", active=True, dm_ok=False)
    db_session.add(supplier)
    await db_session.flush()

    bind_token = await admin_service.create_supplier_bind_token(
        db_session, supplier_id=supplier.id, owner_id=OWNER_TG_ID
    )

    # Use employee's telegram id as the invading id
    conflict_id = 100100100

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(
            update_id=501,
            from_id=conflict_id,
            text=f"/start {bind_token.token}",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(bind_token)
    assert bind_token.used_at is not None

    await db_session.refresh(supplier)
    assert supplier.telegram_id is None


@pytest.mark.asyncio
async def test_hidden_forward_suggests_link(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    supplier = Supplier(name="Ромашка", active=True, dm_ok=False)
    db_session.add(supplier)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_bind_start",
            CallbackData(namespace="admin", action="supplier_bind_start", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_fwd",
            CallbackData(namespace="admin", action="supplier_bind_forward", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    # Hidden forward: forward_origin present but no sender_user
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(
            OWNER_TG_ID,
            update_id=601,
            forward_origin={"type": "channel", "chat": {"id": -100123}},
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.telegram_id is None


@pytest.mark.asyncio
async def test_unbind_supplier_keeps_group_chat(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    supplier = Supplier(name="Ромашка", telegram_id=900900900, active=True, dm_ok=True)
    db_session.add(supplier)
    await db_session.flush()

    private_chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=900900900,
        chat_type=SupplierChatType.private,
        active=True,
        is_default=True,
    )
    group_chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-100500,
        chat_type=SupplierChatType.supergroup,
        active=True,
        is_default=False,
    )
    db_session.add_all([private_chat, group_chat])
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_unbind",
            CallbackData(namespace="admin", action="supplier_unbind", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.telegram_id is None
    assert supplier.dm_ok is False

    await db_session.refresh(private_chat)
    await db_session.refresh(group_chat)
    assert private_chat.active is False
    assert private_chat.is_default is False
    assert group_chat.active is True


@pytest.mark.asyncio
async def test_unknown_private_start_still_ignored(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(update_id=701, from_id=999999999, text="/start"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ignored"
