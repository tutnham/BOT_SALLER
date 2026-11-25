"""parsed_items.sku_key: pre-deploy rows must merge with canonical rows (D7)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ParsedItem, RawPrice
from app.services.price_service import (
    aggregate_today,
    build_sku_key,
    canonical_sku_key,
)


def test_canonical_sku_key_matches_recomputed_predeploy_row() -> None:
    pre_deploy = build_sku_key(
        {
            "model": "18 pro",
            "storage": "512GB",
            "color": "Black",
            "region": "US",
            "sim": None,
        }
    )
    recomputed = canonical_sku_key(
        model="18 pro", storage="512GB", color="Black", region="US", sim=None
    )
    canonical = canonical_sku_key(
        model="iPhone 18 Pro", storage="512GB", color="Black", region="US", sim=None
    )
    assert recomputed == canonical
    assert pre_deploy != canonical


@pytest.mark.asyncio
async def test_sku_key_recompute_merges_today_partitions(
    db_session: AsyncSession, seed_suppliers: list
) -> None:
    raw = RawPrice(
        supplier_id=seed_suppliers[0].id,
        text="18 pro 512 black",
        content_hash="sku-key-canonical-18-pro",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    db_session.add(raw)
    await db_session.flush()

    pre_deploy = ParsedItem(
        raw_price_id=raw.id,
        sku_key=build_sku_key(
            {
                "model": "18 pro",
                "storage": "512GB",
                "color": "Black",
                "region": "US",
                "sim": None,
            }
        ),
        model="18 pro",
        storage="512GB",
        color="Black",
        region="US",
        price=Decimal("60500"),
        currency="RUB",
    )
    post_deploy = ParsedItem(
        raw_price_id=raw.id,
        sku_key=canonical_sku_key(
            model="iPhone 18 Pro", storage="512GB", color="Black", region="US", sim=None
        ),
        model="iPhone 18 Pro",
        storage="512GB",
        color="Black",
        region="US",
        price=Decimal("61000"),
        currency="RUB",
    )
    db_session.add_all([pre_deploy, post_deploy])
    await db_session.flush()

    rows = await aggregate_today(db_session)
    keys = [row["sku_key"] for row in rows]
    assert keys.count(pre_deploy.sku_key) == 1
    assert keys.count(post_deploy.sku_key) == 1

    # Migration 0031 effect: pre-deploy row gets the canonical key.
    pre_deploy.sku_key = canonical_sku_key(
        model="18 pro", storage="512GB", color="Black", region="US", sim=None
    )
    await db_session.flush()

    rows = await aggregate_today(db_session)
    assert len(rows) == 1
    assert rows[0]["sku_key"] == pre_deploy.sku_key
    assert Decimal(str(rows[0]["min_price"])) == Decimal("60500")
