"""Price draft inline buttons and owner approval."""

from __future__ import annotations

import pytest
from app.db.models import ClientGroup, Owner, PriceListDraft
from app.telegram.keyboards import CallbackData
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import PRICE_PUBLISH_CHAT_ID, MockTelegramClient


def _price_callback_payload(
    *,
    from_id: int,
    callback_id: str,
    draft_id: int,
    action: str,
    chat_id: int,
    message_id: int = 42,
) -> dict:
    return {
        "update_id": 920001,
        "callback_query": {
            "id": callback_id,
            "from": {"id": from_id},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": CallbackData(
                namespace="price", action=action, arg=draft_id
            ).encode(),
        },
    }


def _private_price_command(
    *,
    update_id: int,
    from_id: int,
    text: str,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": from_id},
            "chat": {"id": from_id, "type": "private"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_owner_approves_via_button(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner: Owner,
) -> None:
    draft = PriceListDraft(
        items=[{"sku_key": "x", "title": "X", "our_price": "1000.00", "currency": "RUB"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_price_callback_payload(
            from_id=seed_owner.telegram_id,
            callback_id="cb_owner_ok",
            draft_id=draft.id,
            action="approve",
            chat_id=seed_owner.telegram_id,
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    await db_session.refresh(draft)
    assert draft.status == "approved"
    assert draft.approved_by is None
    assert draft.approved_by_telegram_id == seed_owner.telegram_id
    assert any(chat_id == PRICE_PUBLISH_CHAT_ID for chat_id, _, _ in mock_telegram.sent)
    assert mock_telegram.edited


@pytest.mark.asyncio
async def test_employee_approves_via_button(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_employee,
    seed_employee_telegram_id: int,
) -> None:
    draft = PriceListDraft(
        items=[{"sku_key": "y", "title": "Y", "our_price": "2000.00", "currency": "RUB"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_price_callback_payload(
            from_id=seed_employee_telegram_id,
            callback_id="cb_emp_ok",
            draft_id=draft.id,
            action="approve",
            chat_id=seed_employee_telegram_id,
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    await db_session.refresh(draft)
    assert draft.status == "approved"
    assert draft.approved_by == seed_employee.id
    assert draft.approved_by_telegram_id == seed_employee_telegram_id


@pytest.mark.asyncio
async def test_stranger_callback_denied(
    webhook_client: AsyncClient,
    mock_telegram: MockTelegramClient,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_price_callback_payload(
            from_id=888888888,
            callback_id="cb_no",
            draft_id=1,
            action="approve",
            chat_id=888888888,
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.json()["status"] == "ignored"
    assert any(text == "Нет доступа" for _, text, _ in mock_telegram.answer_callbacks)


@pytest.mark.asyncio
async def test_duplicate_approve_button_no_double_publish(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner: Owner,
) -> None:
    draft = PriceListDraft(
        items=[{"sku_key": "z", "title": "Z", "our_price": "3000.00", "currency": "RUB"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    payload = _price_callback_payload(
        from_id=seed_owner.telegram_id,
        callback_id="cb_dup1",
        draft_id=draft.id,
        action="approve",
        chat_id=seed_owner.telegram_id,
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    publish_count = sum(
        1 for chat_id, _, _ in mock_telegram.sent if chat_id == PRICE_PUBLISH_CHAT_ID
    )
    assert publish_count == 1

    mock_telegram.sent.clear()
    payload["callback_query"]["id"] = "cb_dup2"
    payload["update_id"] = 920002
    await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert not any(chat_id == PRICE_PUBLISH_CHAT_ID for chat_id, _, _ in mock_telegram.sent)
    assert any(show_alert for _, _, show_alert in mock_telegram.answer_callbacks)


@pytest.mark.asyncio
async def test_owner_approve_command_in_dm(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner: Owner,
) -> None:
    draft = PriceListDraft(
        items=[{"sku_key": "w", "title": "W", "our_price": "4000.00", "currency": "RUB"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_price_command(
            update_id=920010,
            from_id=seed_owner.telegram_id,
            text=f"/approve_price {draft.id}",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(draft)
    assert draft.status == "approved"
    assert draft.approved_by_telegram_id == seed_owner.telegram_id


@pytest.mark.asyncio
async def test_stranger_price_command_ignored(
    webhook_client: AsyncClient,
    mock_telegram: MockTelegramClient,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_private_price_command(
            update_id=920011,
            from_id=777777777,
            text="/approve_price 1",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.json()["status"] == "ignored"
    assert mock_telegram.sent == []


@pytest.mark.asyncio
async def test_price_command_in_unknown_group_ignored(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee_telegram_id: int,
) -> None:
    draft = PriceListDraft(
        items=[{"sku_key": "u", "title": "U", "our_price": "5000.00", "currency": "RUB"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json={
            "update_id": 920012,
            "message": {
                "message_id": 1,
                "text": f"/approve_price {draft.id}",
                "chat": {"id": -1009999999999, "type": "supergroup"},
                "from": {"id": seed_employee_telegram_id},
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.json()["status"] == "ignored"
