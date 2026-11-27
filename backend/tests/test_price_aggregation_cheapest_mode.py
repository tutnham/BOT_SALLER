"""Ручной прогон прайса берёт минимум между каналами, а не последний пост (A1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Supplier
from app.llm.client import set_llm_client
from app.services.price_service import (
    aggregate_from_raw_price_ids,
    aggregate_latest,
    insert_raw_price,
    parse_raw_prices_for_ids,
)
from tests.conftest import MockLLMClient


def _price_item(model: str, storage: str | None, price: int) -> dict:
    return {
        "items": [
            {
                "model": model,
                "storage": storage,
                "color": "Black",
                "region": "US",
                "sim": None,
                "condition": None,
                "price": price,
                "currency": "RUB",
                "confidence": 0.95,
            }
        ]
    }


async def _channel_price(
    session: AsyncSession,
    mock_llm: MockLLMClient,
    *,
    supplier: Supplier,
    model: str,
    storage: str | None,
    price: int,
    post_date: datetime,
) -> int:
    mock_llm.price_result = _price_item(model, storage, price)
    raw, _is_new = await insert_raw_price(
        session,
        supplier_id=supplier.id,
        text=f"{model} {price}",
        source="channel_post",
        post_date=post_date,
    )
    assert raw is not None
    await parse_raw_prices_for_ids(session, [raw.id])
    return raw.id


@pytest.mark.asyncio
async def test_cheapest_mode_wins_over_newer_higher_price(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    """Заказчик: «18 pro Max 512: в одном прайсе 100, в другом 110 → берём 100»."""
    set_llm_client(mock_llm)
    try:
        cheap = await _channel_price(
            db_session,
            mock_llm,
            supplier=seed_suppliers[0],
            model="iPhone 18 Pro Max",
            storage="512GB",
            price=100000,
            post_date=datetime(2026, 11, 26, 8, 0, tzinfo=UTC),
        )
        dear = await _channel_price(
            db_session,
            mock_llm,
            supplier=seed_suppliers[1],
            model="iPhone 18 Pro Max",
            storage="512GB",
            price=110000,
            post_date=datetime(2026, 11, 26, 9, 0, tzinfo=UTC),
        )

        cheapest = await aggregate_from_raw_price_ids(
            db_session, [cheap, dear], mode="cheapest"
        )
        assert len(cheapest) == 1
        assert Decimal(str(cheapest[0]["min_price"])) == Decimal("100000")
        assert cheapest[0]["supplier_id"] == seed_suppliers[0].id
        assert Decimal(str(cheapest[0]["alt_price"])) == Decimal("110000")

        newest = await aggregate_from_raw_price_ids(db_session, [cheap, dear])
        assert len(newest) == 1
        assert Decimal(str(newest[0]["min_price"])) == Decimal("110000")

        history = await aggregate_latest(db_session)
        assert Decimal(str(history[0]["min_price"])) == Decimal("110000")
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_aggregate_excludes_low_confidence_and_non_rub(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    """Диагностика опирается на эти фильтры: сомнительная цена не может стать прайсом."""
    set_llm_client(mock_llm)
    try:
        mock_llm.price_result = {
            "items": [
                {
                    "model": "iPhone 18 Pro Max",
                    "storage": "512GB",
                    "color": "Black",
                    "region": "US",
                    "sim": None,
                    "condition": None,
                    "price": 1000,
                    "currency": "RUB",
                    "confidence": 0.4,
                },
                {
                    "model": "iPhone 18 Pro Max",
                    "storage": "512GB",
                    "color": "Black",
                    "region": "US",
                    "sim": None,
                    "condition": None,
                    "price": 90000,
                    "currency": "USD",
                    "confidence": 0.95,
                },
                {
                    "model": "iPhone 18 Pro Max",
                    "storage": "512GB",
                    "color": "Black",
                    "region": "US",
                    "sim": None,
                    "condition": None,
                    "price": 105000,
                    "currency": "RUB",
                    "confidence": 0.95,
                },
            ]
        }
        raw, _is_new = await insert_raw_price(
            db_session,
            supplier_id=seed_suppliers[0].id,
            text="iPhone 18 Pro Max 512GB",
            source="channel_post",
            post_date=datetime(2026, 11, 26, 8, 0, tzinfo=UTC),
        )
        assert raw is not None
        await parse_raw_prices_for_ids(db_session, [raw.id])

        rows = await aggregate_from_raw_price_ids(db_session, [raw.id], mode="cheapest")
        assert len(rows) == 1
        assert Decimal(str(rows[0]["min_price"])) == Decimal("105000")
    finally:
        set_llm_client(None)
