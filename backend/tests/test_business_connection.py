"""Webhook business_connection upsert and route deactivation."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BusinessConnection, Supplier, SupplierChat, SupplierChatType
from app.services.admin_service import add_supplier


def _connection_update(
    *,
    update_id: int,
    tg_user_id: int,
    connection_id: str,
    is_enabled: bool = True,
    can_reply: bool = True,
    can_read_messages: bool = True,
) -> dict:
    return {
        "update_id": update_id,
        "business_connection": {
            "id": connection_id,
            "user": {"id": tg_user_id, "first_name": "Client"},
            "is_enabled": is_enabled,
            "rights": {
                "can_reply": can_reply,
                "can_read_messages": can_read_messages,
            },
        },
    }


async def _seed_business_dm(
    db_session: AsyncSession,
    *,
    telegram_id: int,
    connection_id: str,
) -> tuple[Supplier, SupplierChat]:
    supplier = await add_supplier(db_session, name="Biz Supplier", telegram_id=telegram_id)
    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=telegram_id,
        chat_type=SupplierChatType.business_dm,
        business_connection_id=connection_id,
        active=True,
        is_default=False,
    )
    db_session.add(chat)
    await db_session.flush()
    return supplier, chat


@pytest.mark.asyncio
async def test_business_connection_enable(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61001,
            tg_user_id=777001,
            connection_id="bc_enable",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    row = await db_session.scalar(
        select(BusinessConnection).where(BusinessConnection.tg_user_id == 777001)
    )
    assert row is not None
    assert row.business_connection_id == "bc_enable"
    assert row.is_enabled is True
    assert row.can_reply is True
    assert row.can_read_messages is True


@pytest.mark.asyncio
async def test_business_connection_disable_deactivates_routes(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
) -> None:
    _, chat = await _seed_business_dm(
        db_session, telegram_id=777002, connection_id="bc_disable"
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61002,
            tg_user_id=900001,
            connection_id="bc_disable",
        ),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61003,
            tg_user_id=900001,
            connection_id="bc_disable",
            is_enabled=False,
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(chat)
    assert chat.active is False
    assert chat.is_default is False


@pytest.mark.asyncio
async def test_business_connection_can_reply_false_deactivates(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
) -> None:
    _, chat = await _seed_business_dm(
        db_session, telegram_id=777003, connection_id="bc_noreply"
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61004,
            tg_user_id=900002,
            connection_id="bc_noreply",
        ),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61005,
            tg_user_id=900002,
            connection_id="bc_noreply",
            can_reply=False,
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(chat)
    assert chat.active is False


@pytest.mark.asyncio
async def test_business_connection_id_change_deactivates_old(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
) -> None:
    _, chat = await _seed_business_dm(
        db_session, telegram_id=777004, connection_id="bc_old"
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61006,
            tg_user_id=900003,
            connection_id="bc_old",
        ),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_connection_update(
            update_id=61007,
            tg_user_id=900003,
            connection_id="bc_new",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(chat)
    assert chat.active is False
    row = await db_session.scalar(
        select(BusinessConnection).where(BusinessConnection.tg_user_id == 900003)
    )
    assert row is not None
    assert row.business_connection_id == "bc_new"


@pytest.mark.asyncio
async def test_edited_and_deleted_business_updates_no_message_in(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
) -> None:
    edited = await webhook_client.post(
        "/telegram/webhook",
        json={
            "update_id": 61008,
            "edited_business_message": {"message_id": 1, "text": "x"},
        },
        headers=webhook_headers,
    )
    assert edited.json()["status"] == "ignored"
    deleted = await webhook_client.post(
        "/telegram/webhook",
        json={
            "update_id": 61009,
            "deleted_business_messages": {"chat": {"id": 1}, "message_ids": [1, 2]},
        },
        headers=webhook_headers,
    )
    assert deleted.json()["status"] == "ok"
