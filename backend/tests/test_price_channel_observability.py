"""A4: счётчики невидимых потерь прайса — непривязанные и устаревшие каналы, non-RUB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ParsedItem, RawPrice, Supplier
from app.services import price_service
from app.services.parser_client import ParserChannel, ParserClientError
from app.services.parser_diagnostics import diagnose_morning_price
from app.services.price_service import (
    IngestStats,
    count_non_rub_items_today,
    count_stale_bound_channels,
    count_unbound_price_channels,
)


def _channel(pk: int, channel_id: int | None, *, status: str = "active", is_active: bool = True):
    return ParserChannel(
        id=pk,
        channel_id=channel_id,
        username=f"ch{pk}",
        status=status,
        is_active=is_active,
    )


def _diagnose(**overrides) -> list[str]:
    kwargs = {
        "stats": IngestStats(
            raw_new=0, degraded=0, channels_checked=1, posts_seen=2, posts_without_text=0
        ),
        "draft_items": 0,
        "aggregated_items": 0,
        "llm_configured": True,
        "unparsed_raw": 0,
        "low_confidence": 0,
        "confidence_threshold": 0.75,
    }
    kwargs.update(overrides)
    return [reason.code for reason in diagnose_morning_price(**kwargs)]


@pytest.mark.asyncio
async def test_unbound_channels_counted_excluding_bound_and_dead(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Привязанный, ещё не готовый и удалённый канал в счётчик не входят."""

    async def _list():
        return [
            _channel(1, -1005555555555),
            _channel(2, -1009999999999),
            _channel(3, None, status="pending"),
            _channel(4, -1007777777777, is_active=False),
        ]

    monkeypatch.setattr(price_service, "list_channels", _list)

    assert await count_unbound_price_channels(db_session) == 1


@pytest.mark.asyncio
async def test_unbound_count_survives_parser_outage(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom():
        raise ParserClientError("parser_unreachable")

    monkeypatch.setattr(price_service, "list_channels", _boom)

    assert await count_unbound_price_channels(db_session) == 0


@pytest.mark.asyncio
async def test_stale_bound_channel_becomes_fresh_after_sync(
    db_session: AsyncSession, seed_channel_supplier: Supplier
) -> None:
    assert await count_stale_bound_channels(db_session) == 1

    seed_channel_supplier.last_price_sync_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.flush()
    assert await count_stale_bound_channels(db_session) == 1

    seed_channel_supplier.last_price_sync_at = datetime.now(UTC)
    await db_session.flush()
    assert await count_stale_bound_channels(db_session) == 0


@pytest.mark.asyncio
async def test_non_rub_items_counted_only_today(
    db_session: AsyncSession, seed_suppliers: list[Supplier]
) -> None:
    raw = RawPrice(
        supplier_id=seed_suppliers[0].id,
        text="$999",
        content_hash="obs-non-rub",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add_all(
        [
            ParsedItem(
                raw_price_id=raw.id,
                sku_key="iphone 17|256GB|||",
                model="iPhone 17",
                storage="256GB",
                price=Decimal("999"),
                currency="USD",
            ),
            ParsedItem(
                raw_price_id=raw.id,
                sku_key="iphone 17|512GB|||",
                model="iPhone 17",
                storage="512GB",
                price=Decimal("90000"),
                currency="RUB",
            ),
        ]
    )
    await db_session.flush()

    assert await count_non_rub_items_today(db_session) == 1


def test_unbound_and_stale_channels_explain_empty_draft() -> None:
    assert _diagnose(unbound_channels=2, stale_channels=1) == [
        "price_channels_unbound",
        "channel_data_stale",
    ]


def test_partially_fresh_channels_do_not_report_stale() -> None:
    """Один канал из двух свежий — причина не «устарело», а в другом."""
    two_channels = IngestStats(
        raw_new=0, degraded=0, channels_checked=2, posts_seen=2, posts_without_text=0
    )
    assert "channel_data_stale" not in _diagnose(stats=two_channels, stale_channels=1)


def test_non_rub_only_explained_when_nothing_aggregated() -> None:
    assert _diagnose(non_rub_items=3) == ["non_rub_prices_dropped"]
    assert "non_rub_prices_dropped" not in _diagnose(aggregated_items=4, non_rub_items=3)
