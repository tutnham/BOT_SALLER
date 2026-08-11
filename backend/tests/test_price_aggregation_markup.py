"""SKU grouping, min-price, category rules, deterministic our_price."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.db.models import MarkupRule, Supplier
from app.llm.client import set_llm_client
from app.services.markup_service import classify_category, load_rules, resolve_markup
from app.services.price_service import (
    aggregate_today,
    build_draft_items,
    insert_raw_price,
    parse_pending_raw_prices,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import MockLLMClient


def test_classify_category() -> None:
    assert classify_category("iPhone 15 Pro") == "apple"
    assert classify_category("Samsung Galaxy S24") == "samsung"
    assert classify_category("PlayStation 5") == "playstation"
    assert classify_category("Xiaomi 14") == "*"


def test_resolve_markup_fixed_and_fallback() -> None:
    apple = MarkupRule(
        category="apple",
        markup_fixed=Decimal("700"),
        active=True,
    )
    star = MarkupRule(
        category="*",
        markup_fixed=None,
        markup_min=Decimal("500"),
        markup_max=Decimal("1000"),
        active=True,
    )
    rules = {"apple": apple, "*": star}
    assert resolve_markup(rules, "apple", Decimal("500")) == Decimal("700")
    assert resolve_markup(rules, "samsung", Decimal("500")) == Decimal("500")
    assert resolve_markup(rules, "samsung", Decimal("1200")) == Decimal("1000")
    assert resolve_markup({}, "apple", Decimal("500")) is None


@pytest.mark.asyncio
async def test_min_price_aggregation_and_our_price(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_markup_rules: list[MarkupRule],
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)

    # Supplier A — higher price
    mock_llm.price_result = {
        "items": [
            {
                "model": "iPhone 15",
                "storage": "256GB",
                "color": "Black",
                "price": 72000,
                "currency": "RUB",
                "confidence": 0.95,
            }
        ]
    }
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="A: iPhone 15 256 Black 72000",
        source="manual_message",
    )
    await parse_pending_raw_prices(db_session)

    # Supplier B — lower price, same SKU
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
        supplier_id=seed_suppliers[1].id,
        text="B: iPhone 15 256 Black 70000",
        source="manual_message",
    )
    await parse_pending_raw_prices(db_session)

    aggregated = await aggregate_today(db_session)
    assert len(aggregated) == 1
    assert aggregated[0]["sku_key"] == "iPhone 15|256GB|Black||"
    assert Decimal(str(aggregated[0]["min_price"])) == Decimal("70000")

    rules = await load_rules(db_session)
    draft_items = build_draft_items(aggregated, rules, Decimal("500"))
    assert len(draft_items) == 1
    # apple rule markup_fixed=700
    assert draft_items[0]["our_price"] == "70700.00"
    assert "min_price" not in draft_items[0]
    assert "markup" not in draft_items[0]
    assert "supplier" not in draft_items[0]
    set_llm_client(None)


@pytest.mark.asyncio
async def test_low_confidence_excluded_from_aggregation(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)
    mock_llm.price_result = {
        "items": [
            {
                "model": "iPhone 15",
                "storage": "128GB",
                "color": "Blue",
                "price": 65000,
                "currency": "RUB",
                "confidence": 0.2,
            }
        ]
    }
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text="low conf price list",
        source="manual_message",
    )
    inserted = await parse_pending_raw_prices(db_session)
    assert inserted == 1

    aggregated = await aggregate_today(db_session)
    assert aggregated == []
    set_llm_client(None)
