"""Price-channel bind cursor and ingest window without Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.admin_service import (
    bind_supplier_price_channel,
    unbind_supplier_price_channel,
    unbind_suppliers_by_price_channel,
)
from app.services.price_service import (
    CHANNEL_PRICE_CONTENT_TYPES,
    _today_start_utc,
    channel_price_sync_from,
)


def test_channel_price_content_types_include_caption_media() -> None:
    assert CHANNEL_PRICE_CONTENT_TYPES == ("text", "photo", "document", "media_group")


def test_channel_price_sync_from_defaults_to_today() -> None:
    supplier = SimpleNamespace(last_price_sync_at=None)
    assert channel_price_sync_from(supplier, "Europe/Moscow") == _today_start_utc(
        "Europe/Moscow"
    )


def test_channel_price_sync_from_keeps_cursor() -> None:
    cursor = datetime(2026, 11, 15, 21, 0, tzinfo=UTC)
    supplier = SimpleNamespace(last_price_sync_at=cursor)
    assert channel_price_sync_from(supplier, "Europe/Moscow") is cursor


class _EmptyResult:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: [])


class _ListResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._rows))


class _FakeSession:
    def __init__(self, supplier: SimpleNamespace) -> None:
        self.supplier = supplier
        self.flushed = False

    async def get(self, _model, ident):
        if ident == self.supplier.id:
            return self.supplier
        return None

    async def execute(self, _stmt):
        return _EmptyResult()

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
async def test_bind_resets_cursor_when_channel_changes() -> None:
    supplier = SimpleNamespace(
        id=1,
        price_channel_id=-100111,
        price_channel_username="@old",
        last_price_sync_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _FakeSession(supplier)
    await bind_supplier_price_channel(
        session,  # type: ignore[arg-type]
        supplier_id=1,
        channel_id=-100222,
        username="@new",
    )
    assert supplier.price_channel_id == -100222
    assert supplier.last_price_sync_at is None
    assert session.flushed is True


@pytest.mark.asyncio
async def test_bind_keeps_cursor_when_same_channel() -> None:
    cursor = datetime(2026, 1, 1, tzinfo=UTC)
    supplier = SimpleNamespace(
        id=1,
        price_channel_id=-100111,
        price_channel_username="@old",
        last_price_sync_at=cursor,
    )
    session = _FakeSession(supplier)
    await bind_supplier_price_channel(
        session,  # type: ignore[arg-type]
        supplier_id=1,
        channel_id=-100111,
        username="@old",
    )
    assert supplier.last_price_sync_at is cursor


@pytest.mark.asyncio
async def test_unbind_clears_cursor() -> None:
    supplier = SimpleNamespace(
        id=1,
        price_channel_id=-100111,
        price_channel_username="@old",
        last_price_sync_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _FakeSession(supplier)
    await unbind_supplier_price_channel(session, 1)  # type: ignore[arg-type]
    assert supplier.price_channel_id is None
    assert supplier.price_channel_username is None
    assert supplier.last_price_sync_at is None


@pytest.mark.asyncio
async def test_unbind_suppliers_by_price_channel() -> None:
    supplier = SimpleNamespace(
        id=4,
        price_channel_id=-100500,
        price_channel_username="@prices",
        last_price_sync_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    class _Session(_FakeSession):
        async def execute(self, _stmt):
            return _ListResult([supplier])

    session = _Session(supplier)
    count = await unbind_suppliers_by_price_channel(session, -100500)  # type: ignore[arg-type]
    assert count == 1
    assert supplier.price_channel_id is None
    assert supplier.last_price_sync_at is None
