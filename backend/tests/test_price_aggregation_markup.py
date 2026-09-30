"""SKU grouping, min-price, category rules, deterministic our_price."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Supplier
from app.llm.client import set_llm_client
from app.services.markup_service import apply_markup, load_rules, seed_markup_rule_rows
from app.services.price_service import (
    aggregate_today,
    build_draft_items,
    insert_raw_price,
    parse_pending_raw_prices,
)
from tests.conftest import MockLLMClient


def test_apply_markup_from_rules() -> None:
    rules = seed_markup_rule_rows()
    final, markup, rule_id, _key = apply_markup(
        Decimal("70000"),
        "iPhone 15",
        rules,
    )
    assert markup == Decimal("500")
    assert final == Decimal("70500")
    assert markup == Decimal("500")


@pytest.mark.asyncio
async def test_min_price_aggregation_and_our_price(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)

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
    assert Decimal(str(aggregated[0]["min_price"])) == Decimal("70000")

    rules = await load_rules(db_session)
    draft_items = build_draft_items(aggregated, rules, Decimal("500"))
    assert len(draft_items) == 1
    assert draft_items[0]["our_price"] == "70500.00"
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
