"""Draft pending → approve/reject transitions and duplicate protection."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.db.models import MarkupRule, PriceListDraft, Supplier
from app.services.price_service import (
    approve_price_draft,
    insert_raw_price,
    reject_price_draft,
)
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import (
    PRICE_PUBLISH_CHAT_ID,
    MockLLMClient,
    MockTelegramClient,
)


@pytest.mark.asyncio
async def test_draft_created_pending(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
) -> None:
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="iPhone 15 256 Black 70000",
        source="manual_message",
    )
    await db_session.flush()

    with patch(
        "app.services.price_service.get_posts",
        new=AsyncMock(return_value=[]),
    ):
        resp = await webhook_client.post(
            "/jobs/morning-price",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        )

    assert resp.status_code == 200
    draft_id = resp.json()["draft_id"]
    draft = await db_session.get(PriceListDraft, draft_id)
    assert draft is not None
    assert draft.status == "pending"
    assert draft.approved_by is None


@pytest.mark.asyncio
async def test_approve_and_reject_transitions(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_client_group,
) -> None:
    draft = PriceListDraft(
        items=[
            {
                "sku_key": "iPhone 15|256GB|Black||",
                "title": "iPhone 15 256GB Black",
                "our_price": "70700.00",
                "currency": "RUB",
            }
        ],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    # Approve via group command
    resp = await webhook_client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        json={
            "update_id": 910001,
            "message": {
                "message_id": 1,
                "text": f"/approve_price {draft.id}",
                "chat": {"id": seed_group_chat_id, "type": "supergroup"},
                "from": {"id": seed_employee_telegram_id},
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    await db_session.refresh(draft)
    assert draft.status == "approved"
    assert draft.approved_by == seed_employee.id
    assert draft.approved_by_telegram_id == seed_employee_telegram_id
    assert any(chat_id == PRICE_PUBLISH_CHAT_ID for chat_id, _, _ in mock_telegram.sent)
    assert any("утверждён" in text for _, text, _ in mock_telegram.sent)

    # Duplicate approve
    mock_telegram.sent.clear()
    resp2 = await webhook_client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        json={
            "update_id": 910002,
            "message": {
                "message_id": 2,
                "text": f"/approve_price {draft.id}",
                "chat": {"id": seed_group_chat_id, "type": "supergroup"},
                "from": {"id": seed_employee_telegram_id},
            },
        },
    )
    assert resp2.status_code == 200
    await db_session.refresh(draft)
    assert draft.status == "approved"
    assert any("уже обработан" in text for _, text, _ in mock_telegram.sent)
    assert not any(chat_id == PRICE_PUBLISH_CHAT_ID for chat_id, _, _ in mock_telegram.sent)


@pytest.mark.asyncio
async def test_reject_and_duplicate_protection(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_employee,
    seed_employee_telegram_id: int,
) -> None:
    draft = PriceListDraft(
        items=[{"sku_key": "x", "title": "X", "our_price": "1000.00", "currency": "RUB"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    # Reject via employee DM (D5)
    resp = await webhook_client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        json={
            "update_id": 910003,
            "message": {
                "message_id": 3,
                "text": f"/reject_price {draft.id}",
                "chat": {"id": seed_employee_telegram_id, "type": "private"},
                "from": {"id": seed_employee_telegram_id},
            },
        },
    )
    assert resp.status_code == 200
    await db_session.refresh(draft)
    assert draft.status == "rejected"
    assert any("отклонён" in text for _, text, _ in mock_telegram.sent)

    mock_telegram.sent.clear()
    resp2 = await webhook_client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        json={
            "update_id": 910004,
            "message": {
                "message_id": 4,
                "text": f"/reject_price {draft.id}",
                "chat": {"id": seed_employee_telegram_id, "type": "private"},
                "from": {"id": seed_employee_telegram_id},
            },
        },
    )
    assert resp2.status_code == 200
    await db_session.refresh(draft)
    assert draft.status == "rejected"
    assert any("уже обработан" in text for _, text, _ in mock_telegram.sent)


@pytest.mark.asyncio
async def test_approve_not_found(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_employee,
) -> None:
    outcome = await approve_price_draft(
        db_session,
        draft_id=999999,
        employee_id=seed_employee.id,
        telegram=mock_telegram,
    )
    assert outcome == "not_found"

    outcome2 = await reject_price_draft(
        db_session,
        draft_id=999999,
        employee_id=seed_employee.id,
    )
    assert outcome2 == "not_found"
