"""Черновик хранит происхождение цены отдельно от публичных items (A3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule, ParsedItem, RawPrice, Supplier
from app.services.price_service import (
    aggregate_today,
    build_draft_items,
    build_review_sources,
    format_draft_lines,
    format_review_sources_lines,
)

_IPHONE = {"model": "iPhone 18 Pro Max", "storage": "512GB", "color": "Black", "region": "US"}
_SKU_KEY = "iphone 18 pro max|512GB|Black|US|"


async def _add_channel(
    session: AsyncSession, supplier: Supplier, *, username: str, price: int
) -> None:
    supplier.price_channel_username = username
    raw = RawPrice(
        supplier_id=supplier.id,
        text=f"прайс {price}",
        content_hash=f"provenance-{username}",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    session.add(raw)
    await session.flush()
    session.add(
        ParsedItem(
            raw_price_id=raw.id,
            sku_key=_SKU_KEY,
            price=Decimal(str(price)),
            currency="RUB",
            **_IPHONE,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_review_sources_keeps_channel_and_alternative(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
) -> None:
    await _add_channel(db_session, seed_suppliers[1], username="@channel_b", price=110000)
    await _add_channel(db_session, seed_suppliers[0], username="channel_a", price=100000)

    aggregated = await aggregate_today(db_session)
    items = build_draft_items(aggregated, seed_markup_rules, Decimal("500"))
    review = build_review_sources(aggregated, items)

    assert len(items) == 1
    # items — публичный payload: ни канала, ни закупа, ни наценки.
    assert set(items[0]) == {"sku_key", "title", "our_price", "currency"}
    assert items[0]["our_price"] == "100800.00"

    assert len(review) == 1
    row = review[0]
    assert row["our_price"] == "100800.00"
    assert row["purchase"] == "100000.00"
    assert row["channel"] == "@channel_a"
    assert row["alt_purchase"] == "110000.00"
    assert row["alt_channel"] == "@channel_b"
    assert row["markup"] == "800.00"

    lines = format_review_sources_lines(review)
    assert "iPhone 18 Pro Max 512GB Black US — 100 800 ₽" in lines
    assert "@channel_a: закуп 100 000 ₽" in lines
    assert "@channel_b — 110 000 ₽" in lines
    assert "+800 ₽" in lines

    # Сообщение владельцу на одобрение по-прежнему без внутренних данных.
    public = format_draft_lines(items)
    assert "iPhone 18 Pro Max 512GB Black US" in public
    assert "100800.00" in public
    assert "@channel" not in public


@pytest.mark.asyncio
async def test_review_sources_skips_rows_without_draft_item(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
) -> None:
    """Позиция без правила наценки в черновик не попала — и в проверке её нет."""
    aggregated = await aggregate_today(db_session)
    await _add_channel(db_session, seed_suppliers[0], username="@channel_a", price=100000)
    aggregated = await aggregate_today(db_session)
    assert build_review_sources(aggregated, []) == []

    items = build_draft_items(aggregated, seed_markup_rules, Decimal("500"))
    review = build_review_sources(aggregated, items)
    assert [row["sku_key"] for row in review] == [item["sku_key"] for item in items]


def test_review_lines_without_sources() -> None:
    """Черновики, созданные до A3, печатаются как раньше."""
    assert format_review_sources_lines(None) == "—"
    assert (
        format_review_sources_lines([{"sku_key": "x", "title": "iPhone 15", "our_price": "70500.00"}])
        == "iPhone 15 — 70 500 ₽"
    )
