from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClientGroup


def _employee_update(*, update_id: int, from_id: int, group_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": from_id, "first_name": "Employee"},
            "chat": {"id": group_id, "type": "group"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_group_message_ignored_when_group_not_active(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
) -> None:
    db_session.add(
        ClientGroup(chat_id=seed_group_chat_id, title="Inactive", active=False)
    )
    await db_session.flush()

    payload = _employee_update(
        update_id=53001,
        from_id=seed_employee.telegram_id,
        group_id=seed_group_chat_id,
        text="/help",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
