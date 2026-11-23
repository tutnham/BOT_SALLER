"""Full-history manual price: all posts, newest price per sku, no markup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs
from app.db.models import PriceListDraft, RawPrice, Supplier
from app.llm.client import set_llm_client
from app.services.parser_client import ParserPost
from app.services.price_service import (
    aggregate_latest,
    aggregate_today,
    build_full_history_draft_items,
    ingest_channel_prices,
    insert_raw_price,
    parse_pending_raw_prices,
)
from tests.conftest import MockLLMClient


def _price_item(price: int) -> dict:
    return {
        "items": [
            {
                "model": "iPhone 15",
                "storage": "256GB",
                "color": "Black",
                "region": None,
                "sim": None,
                "condition": None,
                "price": price,
                "currency": "RUB",
                "confidence": 0.95,
            }
        ]
    }


@pytest.mark.asyncio
async def test_full_ingest_ignores_cursor_and_stores_post_date(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
) -> None:
    cursor = datetime(2026, 11, 20, tzinfo=UTC)
    seed_channel_supplier.last_price_sync_at = cursor
    await db_session.flush()

    post_date = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    posts = [
        ParserPost(
            post_id=1,
            message_id=1,
            post_date=post_date,
            raw_text="iPhone 15 256GB Black 70000",
            message_link="https://t.me/c/1/1",
        )
    ]
    captured: dict = {}

    async def _fake_iter_posts(**kwargs):
        captured.update(kwargs)
        yield posts

    with patch("app.services.price_service.iter_posts", new=_fake_iter_posts):
        stats = await ingest_channel_prices(db_session, full_history=True)

    assert captured["from_"] is None
    assert captured["page_size"] == 500
    assert stats.raw_new == 1

    raw = (
        await db_session.execute(select(RawPrice).where(RawPrice.source == "channel_post"))
    ).scalar_one()
    assert raw.post_date == post_date
    # Курсор не откатывается назад старыми постами полной истории.
    await db_session.refresh(seed_channel_supplier)
    assert seed_channel_supplier.last_price_sync_at == cursor

    async def _fake_iter_default(**kwargs):
        captured.update(kwargs)
        return
        yield  # pragma: no cover

    with patch("app.services.price_service.iter_posts", new=_fake_iter_default):
        await ingest_channel_prices(db_session)
    assert captured["from_"] == cursor


@pytest.mark.asyncio
async def test_aggregate_latest_picks_newest_post_and_no_markup(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)

    mock_llm.price_result = _price_item(70000)
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="old: iPhone 15 256 Black 70000",
        source="channel_post",
        post_date=datetime(2026, 11, 1, tzinfo=UTC),
    )
    await parse_pending_raw_prices(db_session)

    mock_llm.price_result = _price_item(72000)
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="new: iPhone 15 256 Black 72000",
        source="channel_post",
        post_date=datetime(2026, 11, 20, tzinfo=UTC),
    )
    await parse_pending_raw_prices(db_session)

    latest = await aggregate_latest(db_session)
    assert len(latest) == 1
    assert Decimal(str(latest[0]["min_price"])) == Decimal("72000")

    draft_items = build_full_history_draft_items(latest)
    assert len(draft_items) == 1
    assert draft_items[0]["our_price"] == "72000.00"

    # Крон-агрегация не изменилась: минимум за сегодня.
    today = await aggregate_today(db_session)
    assert len(today) == 1
    assert Decimal(str(today[0]["min_price"])) == Decimal("70000")
    set_llm_client(None)


@pytest.mark.asyncio
async def test_parse_include_history_covers_old_raws(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)
    mock_llm.price_result = _price_item(70000)

    raw, _is_new = await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="ancient price list",
        source="channel_post",
        post_date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert raw is not None
    await db_session.execute(
        update(RawPrice)
        .where(RawPrice.id == raw.id)
        .values(received_at=datetime.now(UTC) - timedelta(days=30))
    )
    await db_session.flush()

    assert await parse_pending_raw_prices(db_session) == 0
    assert await parse_pending_raw_prices(db_session, include_history=True) == 1
    set_llm_client(None)


@pytest.mark.asyncio
async def test_manual_full_history_job_draft_without_markup(
    db_session: AsyncSession,
    mock_telegram,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
) -> None:
    set_llm_client(mock_llm)
    mock_llm.price_result = _price_item(70000)

    posts = [
        ParserPost(
            post_id=101,
            message_id=101,
            post_date=datetime(2026, 7, 30, 7, 0, tzinfo=UTC),
            raw_text="iPhone 15 256GB Black 70000",
            message_link="https://t.me/c/1/101",
        )
    ]

    async def _fake_iter_posts(**kwargs):
        yield posts

    with patch("app.services.price_service.iter_posts", new=_fake_iter_posts):
        result = await jobs.run_morning_price(
            db_session, mock_telegram, full_history=True
        )

    assert result["status"] == "ok"
    assert result["draft_id"] is not None
    assert result["items"] == 1

    draft = await db_session.get(PriceListDraft, result["draft_id"])
    assert draft is not None
    assert draft.status == "pending"
    assert draft.items[0]["our_price"] == "70000.00"

    approval_msgs = [
        text for _chat_id, text, _markup in mock_telegram.sent if "/approve_price" in text
    ]
    assert len(approval_msgs) == 1
    set_llm_client(None)