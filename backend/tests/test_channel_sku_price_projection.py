"""Проекция channel_sku_prices: минимум дня, свежий upsert, срок годности."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ChannelSkuPrice, Supplier
from app.llm.client import set_llm_client
from app.services import channel_sku_price_service
from app.services.channel_sku_price_service import (
    get_fresh_channel_price,
    rebuild_channel_sku_prices,
)
from app.services.price_service import (
    _project_channel_prices,
    aggregate_from_raw_price_ids,
    aggregate_today,
    insert_raw_price,
    parse_raw_prices_for_ids,
)
from tests.conftest import MockLLMClient

KEY_15 = "v1|apple|iphone|15|256|black|*|*"
TODAY = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)


def _price_result(price: int) -> dict:
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


async def _seed_channel_price(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    supplier: Supplier,
    price: int,
    *,
    post_date: datetime,
) -> int:
    """Один пост канала → raw_price + parsed_items; возвращает id raw_price."""
    mock_llm.price_result = _price_result(price)
    raw, _is_new = await insert_raw_price(
        db_session,
        supplier_id=supplier.id,
        text=f"{supplier.name} {post_date:%m-%d}: iPhone 15 256GB Black {price}",
        source="channel_post",
        post_date=post_date,
    )
    assert raw is not None
    await parse_raw_prices_for_ids(db_session, [raw.id])
    return raw.id


async def _projection_rows(db_session: AsyncSession) -> list[ChannelSkuPrice]:
    return list((await db_session.scalars(select(ChannelSkuPrice))).all())


@pytest.fixture
def projection_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNEL_SKU_PRICE_PROJECTION_ENABLED", "true")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_projection_holds_cheapest_channel_price(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    set_llm_client(mock_llm)
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_suppliers[1], 110000, post_date=TODAY
        )
        await _seed_channel_price(
            db_session, mock_llm, seed_suppliers[0], 100000, post_date=TODAY
        )

        aggregated = await aggregate_today(db_session)
        assert len(aggregated) == 1
        assert await rebuild_channel_sku_prices(db_session, aggregated, now=TODAY) == 1

        row = (await _projection_rows(db_session)).pop()
        assert row.canonical_sku_key == KEY_15
        assert row.sku_key == aggregated[0]["sku_key"]
        assert row.purchase_unit_price == Decimal("100000.00")
        assert row.client_unit_price == Decimal("100500.00")
        assert row.markup_rub == Decimal("500.00")
        assert row.supplier_id == seed_suppliers[0].id
        assert row.parsed_item_id is not None
        assert row.normalizer_version == "v1"
        assert row.selection_version == 1
        assert row.invalidated_at is None
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_rerun_and_older_post_do_not_clobber_projection(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    """Тот же снимок и более старый канал дороже не перебивают сегодняшнюю цену."""
    set_llm_client(mock_llm)
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_suppliers[0], 100000, post_date=TODAY
        )
        stale_raw = await _seed_channel_price(
            db_session,
            mock_llm,
            seed_suppliers[1],
            110000,
            post_date=TODAY - timedelta(days=2),
        )

        assert await rebuild_channel_sku_prices(
            db_session, await aggregate_today(db_session), now=TODAY
        ) == 1
        # Повторный полный срез обновляет строку (наценка могла смениться), цена та же.
        assert await rebuild_channel_sku_prices(
            db_session, await aggregate_today(db_session), now=TODAY
        ) == 1
        # Только старый пост (дороже) — проекция остаётся прежней.
        stale_scope = await aggregate_from_raw_price_ids(db_session, [stale_raw])
        assert await rebuild_channel_sku_prices(db_session, stale_scope, now=TODAY) == 0

        row = (await _projection_rows(db_session)).pop()
        assert row.purchase_unit_price == Decimal("100000.00")
        assert row.supplier_id == seed_suppliers[0].id
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_newer_post_reprices_projection(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
) -> None:
    """Канал исправил цену: более свежее наблюдение пересобирает строку."""
    set_llm_client(mock_llm)
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 100000, post_date=TODAY
        )
        assert await rebuild_channel_sku_prices(
            db_session, await aggregate_today(db_session), now=TODAY
        ) == 1

        later = TODAY + timedelta(hours=3)
        raised_raw = await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 104000, post_date=later
        )
        raised = await aggregate_from_raw_price_ids(db_session, [raised_raw])
        assert await rebuild_channel_sku_prices(db_session, raised, now=later) == 1

        row = (await _projection_rows(db_session)).pop()
        assert row.purchase_unit_price == Decimal("104000.00")
        assert row.client_unit_price == Decimal("104500.00")
        assert row.channel_id == seed_channel_supplier.price_channel_id
        assert row.selection_version == 2
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_fresh_price_needs_flag_date_and_expiry(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_llm_client(mock_llm)
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 100000, post_date=TODAY
        )
        await rebuild_channel_sku_prices(
            db_session, await aggregate_today(db_session), now=TODAY
        )
        assert await get_fresh_channel_price(
            db_session, canonical_sku_key=KEY_15, now=TODAY
        ) is None

        monkeypatch.setenv("CHANNEL_SKU_PRICE_PROJECTION_ENABLED", "true")
        get_settings.cache_clear()
        fresh = await get_fresh_channel_price(db_session, canonical_sku_key=KEY_15, now=TODAY)
        assert fresh is not None
        assert fresh.client_unit_price == Decimal("100500.00")

        fresh.expires_at = TODAY + timedelta(hours=1)
        await db_session.flush()
        assert await get_fresh_channel_price(
            db_session, canonical_sku_key=KEY_15, now=TODAY + timedelta(hours=2)
        ) is None
        fresh.expires_at = TODAY + timedelta(days=30)
        fresh.invalidated_at = TODAY
        await db_session.flush()
        assert await get_fresh_channel_price(
            db_session, canonical_sku_key=KEY_15, now=TODAY
        ) is None
    finally:
        set_llm_client(None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_morning_price_hook_writes_only_under_flag(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_llm_client(mock_llm)
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 100000, post_date=TODAY
        )
        aggregated = await aggregate_today(db_session)

        await _project_channel_prices(db_session, aggregated)
        assert await _projection_rows(db_session) == []

        monkeypatch.setenv("CHANNEL_SKU_PRICE_PROJECTION_ENABLED", "true")
        get_settings.cache_clear()
        await _project_channel_prices(db_session, aggregated)
        assert (await _projection_rows(db_session)).pop().canonical_sku_key == KEY_15
    finally:
        set_llm_client(None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_projection_failure_keeps_draft_path_alive(
    db_session: AsyncSession,
    projection_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой проекции не отменяет черновик: savepoint откатывает только её."""

    async def _boom(*args, **kwargs) -> int:
        raise RuntimeError("projection_broke")

    monkeypatch.setattr(
        channel_sku_price_service, "rebuild_channel_sku_prices", _boom, raising=False
    )
    row = {
        "sku_key": "iphone 15|256gb|black|none|none",
        "model": "iPhone 15",
        "storage": "256GB",
        "min_price": Decimal("100000"),
        "currency": "RUB",
        "supplier_id": 1,
    }
    await _project_channel_prices(db_session, [row])

    assert await _projection_rows(db_session) == []
