"""Tests for supplier replies arriving in a bound group chat."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, MessageIn, Supplier, SupplierChat, SupplierChatType

SUPPLIER_TG_ID = 200200201


def _group_message_payload(
    chat_id: int,
    from_id: int,
    text: str,
    *,
    update_id: int = 100,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": from_id},
            "chat": {"id": chat_id, "type": "supergroup"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_supplier_reply_in_bound_group(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    supplier = seed_suppliers[0]
    group_id = -1009999999999
    db_session.add(
        SupplierChat(
            supplier_id=supplier.id,
            chat_id=group_id,
            chat_type=SupplierChatType.supergroup,
            active=True,
            is_default=False,
        )
    )
    await db_session.flush()

    # Create an outgoing ask to bind request to supplier
    from app.services.request_service import create_request
    from app.telegram.client import get_telegram_client

    request, _ = await create_request(
        db_session,
        group_chat_id=-1001234567890,
        employee_id=seed_employee.id,
        source_text="iPhone 15 256GB black",
        telegram=get_telegram_client(),
    )

    payload = _group_message_payload(
        group_id,
        SUPPLIER_TG_ID,
        f"{request.id} 85000",
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    msg_in = (
        await db_session.execute(
            select(MessageIn).where(MessageIn.chat_id == group_id)
        )
    ).scalar_one_or_none()
    assert msg_in is not None
    assert msg_in.supplier_id == supplier.id
