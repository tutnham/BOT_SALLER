"""Outgoing draft must not expose supplier, purchase price, or markup internals."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.db.models import MarkupRule, PriceListDraft, Supplier
from app.services.price_service import insert_raw_price
from app.templates.messages_ru import render_template
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import PRICE_APPROVAL_CHAT_ID, MockLLMClient

FORBIDDEN_FRAGMENTS = (
    "Supplier One",
    "Supplier Two",
    "Channel Supplier",
    "закуп",
    "наценк",
    "markup",
    "min_price",
    "70000",  # purchase price from LLM mock before markup
)


@pytest.mark.asyncio
async def test_draft_message_hides_sensitive_fields(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram,
    mock_llm: MockLLMClient,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
) -> None:
    mock_llm.price_result = {
        "items": [
            {
                "model": "iPhone 15",
                "storage": "256GB",
                "color": "Black",
                "price": 70000,
                "currency": "RUB",
                "confidence": 0.95,
            }
        ]
    }
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="secret supplier price list body unique",
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

    for item in draft.items:
        for key in ("supplier_id", "supplier", "min_price", "markup", "markup_fixed"):
            assert key not in item
        assert "our_price" in item
        assert "title" in item

    approval_texts = [
        text
        for chat_id, text in mock_telegram.sent
        if chat_id == PRICE_APPROVAL_CHAT_ID
    ]
    assert approval_texts
    outgoing = "\n".join(approval_texts)
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment.lower() not in outgoing.lower()

    # Public price after iphone markup 500: 70500
    assert "70500" in outgoing
    assert "/approve_price" in outgoing
    assert "/reject_price" in outgoing


@pytest.mark.asyncio
async def test_draft_also_notifies_dm_ok_owners(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram,
    mock_llm: MockLLMClient,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
    seed_owner,
) -> None:
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="iPhone 15 256 Black owner notify unique",
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
    approval_chats = {
        chat_id
        for chat_id, text in mock_telegram.sent
        if "/approve_price" in text
    }
    assert PRICE_APPROVAL_CHAT_ID in approval_chats
    assert seed_owner.telegram_id in approval_chats


def test_published_template_has_no_internals() -> None:
    text = render_template(
        "price_list_published",
        items_block="iPhone 15 256GB Black — 70700.00 ₽",
    )
    assert "70700" in text
    for fragment in ("markup", "min_price", "supplier", "наценк"):
        assert fragment not in text.lower()
