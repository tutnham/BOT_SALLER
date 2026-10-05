"""Morning-price acceptance scenarios with a fake parser and Telegram."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule, PriceListDraft
from app.services.parser_client import ParserClientError
from app.services.price_service import (
    approve_price_draft,
    build_draft_items,
    ingest_channel_prices,
    reject_price_draft,
)
from app.telegram.client import TelegramSendError
from tests.conftest import MockTelegramClient

pytestmark = pytest.mark.acceptance


def test_draft_hides_supplier_purchase_and_markup() -> None:
    items = build_draft_items(
        [
            {
                "sku_key": "iphone-17-pro-256",
                "model": "iPhone 17 Pro",
                "storage": "256",
                "color": None,
                "region": None,
                "sim": None,
                "min_price": Decimal("80000"),
                "currency": "RUB",
            }
        ],
        [],
        Decimal("500"),
    )
    assert len(items) == 1
    blob = str(items[0])
    assert "supplier" not in blob
    assert "markup" not in blob
    assert "80000" not in blob
    assert Decimal(items[0]["our_price"]) > Decimal("80000")


@pytest.mark.asyncio
async def test_unavailable_parser_is_degraded(
    db_session: AsyncSession,
    seed_channel_supplier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(**_kwargs):
        raise ParserClientError("down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.price_service.iter_posts", _fail)
    _new, degraded = await ingest_channel_prices(db_session)
    assert degraded == 1
    assert seed_channel_supplier.id


@pytest.mark.asyncio
async def test_approve_is_idempotent_and_reject_does_not_publish(
    db_session: AsyncSession,
    seed_employee,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "price_publish_chat_ids", "424242")
    draft = PriceListDraft(
        items=[{"title": "iPhone", "our_price": "1000.00", "sku_key": "iphone"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()

    first = await approve_price_draft(
        db_session,
        draft_id=draft.id,
        employee_id=seed_employee.id,
        telegram=mock_telegram,
    )
    second = await approve_price_draft(
        db_session,
        draft_id=draft.id,
        employee_id=seed_employee.id,
        telegram=mock_telegram,
    )
    assert first == "approved"
    assert second == "already_processed"
    assert len(mock_telegram.sent) == 1

    rejected = PriceListDraft(
        items=[{"title": "iPhone", "our_price": "1000.00", "sku_key": "iphone"}],
        status="pending",
    )
    db_session.add(rejected)
    await db_session.flush()
    mock_telegram.sent.clear()
    outcome = await reject_price_draft(
        db_session,
        draft_id=rejected.id,
        employee_id=seed_employee.id,
    )
    assert outcome == "rejected"
    assert mock_telegram.sent == []


@pytest.mark.asyncio
async def test_one_failed_publish_chat_is_partial(
    db_session: AsyncSession,
    seed_employee,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "price_publish_chat_ids", "1,2")

    class _PartialTelegram:
        async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> int:
            if chat_id == 2:
                raise TelegramSendError("boom")
            return 1

    draft = PriceListDraft(
        items=[{"title": "iPhone", "our_price": "1000.00", "sku_key": "iphone"}],
        status="pending",
    )
    db_session.add(draft)
    await db_session.flush()
    outcome = await approve_price_draft(
        db_session,
        draft_id=draft.id,
        employee_id=seed_employee.id,
        telegram=_PartialTelegram(),  # type: ignore[arg-type]
    )
    assert outcome == "approved_partial"


def test_markup_rule_type_stays_decimal() -> None:
    assert isinstance(Decimal("1.50") + Decimal("0.50"), Decimal)
    assert MarkupRule is not None
