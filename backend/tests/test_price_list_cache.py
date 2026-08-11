"""parse_cache for kind=price_list — hit skips LLM, miss stores cache."""

from __future__ import annotations

import pytest
from app.db.models import ParseCache, Supplier
from app.llm.client import set_llm_client
from app.parsers.cache import build_content_hash, set_cached
from app.services.price_service import insert_raw_price, parse_pending_raw_prices
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import MockLLMClient


@pytest.mark.asyncio
async def test_price_list_cache_hit_skips_llm(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)
    text = "iPhone 15 256GB Black 70000"
    await set_cached(
        db_session,
        kind="price_list",
        raw_text=text,
        result_json={
            "items": [
                {
                    "model": "iPhone 15",
                    "storage": "256GB",
                    "color": "Black",
                    "price": 70000,
                    "currency": "RUB",
                    "confidence": 0.99,
                }
            ]
        },
        model_used="mock",
    )
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text=text,
        source="manual_message",
    )
    await db_session.flush()

    inserted = await parse_pending_raw_prices(db_session)
    assert inserted == 1
    assert mock_llm.price_calls == 0
    set_llm_client(None)


@pytest.mark.asyncio
async def test_price_list_cache_miss_calls_llm_and_stores(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    mock_llm: MockLLMClient,
) -> None:
    set_llm_client(mock_llm)
    text = "Galaxy S24 Ultra 512 90000"
    mock_llm.price_result = {
        "items": [
            {
                "model": "Galaxy S24 Ultra",
                "storage": "512",
                "color": None,
                "price": 90000,
                "currency": "RUB",
                "confidence": 0.9,
            }
        ]
    }
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[0].id,
        text=text,
        source="manual_message",
    )
    await db_session.flush()

    inserted = await parse_pending_raw_prices(db_session)
    assert inserted == 1
    assert mock_llm.price_calls == 1

    content_hash = build_content_hash("price_list", text)
    cached = await db_session.scalar(
        select(ParseCache).where(ParseCache.content_hash == content_hash)
    )
    assert cached is not None
    assert cached.kind == "price_list"
    assert cached.result_json["items"][0]["model"] == "Galaxy S24 Ultra"

    # Second parse of same text via new raw_price with different supplier text
    # but same normalized content for cache key — reuse cache
    mock_llm.price_calls = 0
    await insert_raw_price(
        db_session,
        supplier_id=seed_suppliers[1].id,
        text=text,
        source="manual_message",
    )
    await db_session.flush()
    inserted2 = await parse_pending_raw_prices(db_session)
    assert inserted2 == 1
    assert mock_llm.price_calls == 0

    cache_count = await db_session.scalar(select(func.count()).select_from(ParseCache))
    assert cache_count == 1
    set_llm_client(None)
