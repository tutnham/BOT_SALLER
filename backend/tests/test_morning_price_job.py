"""POST /jobs/morning-price — auth, happy path, safe re-run."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule, PriceListDraft, RawPrice, Supplier
from app.services.parser_client import ParserPost
from app.services.price_service import insert_raw_price


@pytest.mark.asyncio
async def test_morning_price_requires_secret(webhook_client: AsyncClient) -> None:
    resp = await webhook_client.post("/jobs/morning-price", json={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_morning_price_happy_path_manual_and_channel(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram,
    mock_llm,
    seed_suppliers: list[Supplier],
    seed_channel_supplier: Supplier,
    seed_markup_rules: list[MarkupRule],
) -> None:
    manual_supplier = seed_suppliers[0]
    await insert_raw_price(
        db_session,
        supplier_id=manual_supplier.id,
        text="iPhone 15 256GB Black 70000",
        source="manual_message",
    )
    await db_session.flush()

    mock_llm.price_result = {
        "items": [
            {
                "model": "iPhone 15",
                "storage": "256GB",
                "color": "Black",
                "region": None,
                "sim": None,
                "condition": None,
                "price": 70000,
                "currency": "RUB",
                "confidence": 0.95,
            }
        ]
    }

    posts = [
        ParserPost(
            post_id=101,
            message_id=101,
            post_date=datetime(2026, 7, 30, 7, 0, tzinfo=UTC),
            raw_text="iPhone 15 256GB Black 69500",
            message_link="https://t.me/c/1/101",
        )
    ]

    async def _fake_get_posts(**kwargs):
        return posts

    with patch(
        "app.services.price_service.get_posts",
        new=AsyncMock(side_effect=_fake_get_posts),
    ):
        resp = await webhook_client.post(
            "/jobs/morning-price",
            json={},
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["draft_id"] is not None
    assert body["items"] >= 1

    draft = await db_session.get(PriceListDraft, body["draft_id"])
    assert draft is not None
    assert draft.status == "pending"
    assert isinstance(draft.items, list)
    assert draft.items[0]["our_price"]
    assert "supplier" not in str(draft.items).lower()

    await db_session.refresh(seed_channel_supplier)
    assert seed_channel_supplier.last_price_sync_at is not None

    approval_msgs = [
        text for chat_id, text, markup in mock_telegram.sent if "/approve_price" in text
    ]
    assert approval_msgs
    markup = next(markup for _, text, markup in mock_telegram.sent if "/approve_price" in text)
    callbacks = [
        btn["callback_data"]
        for row in (markup or {}).get("inline_keyboard", [])
        for btn in row
        if btn.get("callback_data")
    ]
    assert any(data.startswith("price:approve:") for data in callbacks)
    assert any(data.startswith("price:reject:") for data in callbacks)
    assert all(len(data.encode("utf-8")) <= 64 for data in callbacks)


@pytest.mark.asyncio
async def test_morning_price_safe_rerun(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_llm,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
) -> None:
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="iPhone 15 256GB Black 70000",
        source="manual_message",
    )
    await db_session.flush()

    with patch(
        "app.services.price_service.get_posts",
        new=AsyncMock(return_value=[]),
    ):
        first = await webhook_client.post(
            "/jobs/morning-price",
            json={},
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )
        second = await webhook_client.post(
            "/jobs/morning-price",
            json={},
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["draft_id"] == second.json()["draft_id"]
    assert second.json().get("reused") is True

    drafts_count = await db_session.scalar(
        select(func.count()).select_from(PriceListDraft)
    )
    assert drafts_count == 1

    raw_count = await db_session.scalar(select(func.count()).select_from(RawPrice))
    assert raw_count == 1
