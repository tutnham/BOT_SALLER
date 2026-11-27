"""Ключ parsed_items для не-iPhone: один товар = один sku_key (A2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ParsedItem, RawPrice, Supplier
from app.parsers.product_normalizer import sku_key_fields_strict
from app.services.price_service import (
    aggregate_today,
    build_sku_key,
    canonical_sku_key,
)


def _key(
    model: str,
    storage: str | None,
    *,
    color: str | None = None,
    region: str | None = None,
    sim: str | None = None,
) -> str:
    return canonical_sku_key(
        model=model,
        storage=storage,
        color=color,
        region=region,
        sim=sim,
        strict=True,
    )


def test_same_samsung_position_from_two_channels_shares_one_key() -> None:
    assert _key("Galaxy S25 Ultra", "512GB", color="Black", region="US") == _key(
        "Самсунг Galaxy S25 Ultra 512", None, color="black", region="us"
    )


def test_bare_model_number_merges_with_brand_named_row() -> None:
    """Строки до канонизации iPhone («18 pro») должны сходиться с новыми."""
    assert _key("18 pro", "512GB", color="Black", region="US") == _key(
        "iPhone 18 Pro", "512", color="Black", region="US"
    )


def test_one_terrabyte_and_1024_gigabytes_are_the_same_position() -> None:
    assert _key("iPhone 17 Pro", "1TB") == _key("iPhone 17 Pro", "1024GB")


def test_cyrillic_color_alias_merges() -> None:
    """Русские прайсы пишут и «черный», и «чёрный»: это одна позиция."""
    assert _key("iPhone 18 Pro Max", "512GB", color="черный") == _key(
        "iPhone 18 Pro Max", "512GB", color="ЧЁРНЫЙ"
    )


def test_titanium_finishes_and_laptop_ssd_stay_distinct() -> None:
    natural = _key("iPhone 16 Pro", "256GB", color="Natural Titanium")
    desert = _key("iPhone 16 Pro", "256GB", color="Desert Titanium")
    assert natural != desert
    assert _key("iPhone 16 Pro", "256GB", color="Black") == _key(
        "iPhone 16 Pro", "256GB", color="черный"
    )
    ram256 = _key("MacBook Pro 14 M4 32/256", None)
    ram512 = _key("MacBook Pro 14 M4 32/512", None)
    assert ram256 != ram512
    assert "256GB" in ram256
    assert "512GB" in ram512
    assert "32" in ram256 and "32" in ram512


def test_different_storage_stays_different() -> None:
    assert _key("iPhone 18 Pro Max", "256GB") != _key("iPhone 18 Pro Max", "512GB")


def test_number_without_unit_that_is_not_storage_stays_in_model() -> None:
    """«WH-1000XM4» — модель, а не накопитель: фантомный 1000GB в ключе недопустим."""
    fields = sku_key_fields_strict(
        model="Sony WH-1000XM4", storage=None, color=None, region=None, sim=None
    )
    assert fields["storage"] is None
    assert "1000" in (fields["model"] or "")


def test_strict_off_keeps_the_0031_shape() -> None:
    legacy = canonical_sku_key(
        model="Galaxy S25 Ultra", storage="512GB", color="Black", region="US", sim=None
    )
    assert legacy == build_sku_key(
        {"model": "Galaxy S25 Ultra", "storage": "512GB", "color": "Black", "region": "US", "sim": None}
    )
    assert legacy != _key("Galaxy S25 Ultra", "512GB", color="Black", region="US")


@pytest.mark.asyncio
async def test_two_channels_merge_into_one_minimum(
    db_session: AsyncSession, seed_suppliers: list[Supplier]
) -> None:
    """Без строгого ключа здесь были бы две строки по 110 000 и 100 000."""
    raw_a = RawPrice(
        supplier_id=seed_suppliers[0].id,
        text="Galaxy S25 Ultra 512 100000",
        content_hash="slug-samsung-a",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    raw_b = RawPrice(
        supplier_id=seed_suppliers[1].id,
        text="Самсунг Galaxy S25 Ultra 512GB 110000",
        content_hash="slug-samsung-b",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    db_session.add_all([raw_a, raw_b])
    await db_session.flush()

    db_session.add_all(
        [
            ParsedItem(
                raw_price_id=raw_a.id,
                sku_key=_key("Galaxy S25 Ultra", "512", color="Black", region="US"),
                model="Galaxy S25 Ultra",
                storage="512",
                color="Black",
                region="US",
                price=Decimal("100000"),
                currency="RUB",
            ),
            ParsedItem(
                raw_price_id=raw_b.id,
                sku_key=_key("Самсунг Galaxy S25 Ultra 512GB", None, color="black", region="us"),
                model="Самсунг Galaxy S25 Ultra 512GB",
                storage=None,
                color="black",
                region="us",
                price=Decimal("110000"),
                currency="RUB",
            ),
        ]
    )
    await db_session.flush()

    rows = await aggregate_today(db_session)
    assert len(rows) == 1
    assert Decimal(str(rows[0]["min_price"])) == Decimal("100000")
    assert rows[0]["supplier_id"] == seed_suppliers[0].id
    assert Decimal(str(rows[0]["alt_price"])) == Decimal("110000")
