"""Private /start onboarding and dm_ok gate (TECH DOC §5, §15 case 4)."""

from __future__ import annotations

import pytest
from app.db.models import MessageIn, Owner, Supplier
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _private_update(
    *,
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


@pytest.mark.asyncio
async def test_owner_start_sets_dm_ok(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram,
) -> None:
    owner = Owner(telegram_id=300300301, name="Owner Pending", dm_ok=False)
    db_session.add(owner)
    await db_session.flush()
    mock_telegram.sent.clear()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(update_id=55001, from_id=owner.telegram_id, text="/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    await db_session.refresh(owner)
    assert owner.dm_ok is True
    assert any("Доступ к отчётам открыт" in txt for _, txt in mock_telegram.sent)


@pytest.mark.asyncio
async def test_owner_start_idempotent(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram,
) -> None:
    owner = Owner(telegram_id=300300302, name="Owner Ready", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()
    mock_telegram.sent.clear()

    for update_id in (55002, 55003):
        resp = await webhook_client.post(
            "/telegram/webhook",
            json=_private_update(
                update_id=update_id,
                from_id=owner.telegram_id,
                text="/start",
            ),
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    await db_session.refresh(owner)
    assert owner.dm_ok is True


@pytest.mark.asyncio
async def test_supplier_start_sets_dm_ok_without_messages_in(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    supplier = seed_suppliers[3]
    supplier.dm_ok = False
    await db_session.flush()
    mock_telegram.sent.clear()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(
            update_id=55004,
            from_id=supplier.telegram_id or 0,
            text="/start",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.dm_ok is True
    assert any("поставщик" in txt.lower() for _, txt in mock_telegram.sent)

    messages_in = await db_session.scalars(select(MessageIn))
    assert list(messages_in) == []


@pytest.mark.asyncio
async def test_unknown_private_start_ignored(
    webhook_client: AsyncClient,
    mock_telegram,
) -> None:
    mock_telegram.sent.clear()
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(update_id=55005, from_id=999999999, text="/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert mock_telegram.sent == []


@pytest.mark.asyncio
async def test_owner_report_requires_start_then_works(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram,
) -> None:
    owner = Owner(telegram_id=300300303, name="Owner Report", dm_ok=False)
    db_session.add(owner)
    await db_session.flush()
    mock_telegram.sent.clear()

    blocked = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(
            update_id=55006,
            from_id=owner.telegram_id,
            text="/report day",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "ignored"
    assert mock_telegram.sent == []

    started = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(update_id=55007, from_id=owner.telegram_id, text="/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "ok"

    mock_telegram.sent.clear()
    report = await webhook_client.post(
        "/telegram/webhook",
        json=_private_update(
            update_id=55008,
            from_id=owner.telegram_id,
            text="/report day",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert report.status_code == 200
    assert report.json()["status"] == "ok"
    assert len(mock_telegram.sent) == 1
