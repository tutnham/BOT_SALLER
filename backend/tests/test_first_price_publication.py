"""First valid price is published to the client exactly once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Quote,
    QuoteSource,
    Request,
    RequestPriceSelection,
    RequestStatus,
    Supplier,
    TelegramOutbox,
)
from app.services.first_price_publication_service import (
    first_client_price_key,
    publish_first_valid_price,
)
from app.services.quote_service import upsert_quote


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_PUBLISH_FIRST_VALID_PRICE", "true")
    get_settings.cache_clear()


async def _request(session: AsyncSession, employee_id: int, *, status=RequestStatus.awaiting_answers) -> Request:
    request = Request(
        group_chat_id=-1001,
        employee_id=employee_id,
        source_text="iPhone 17 Air 256GB Black",
        status=status,
        canonical_sku_key="v1|apple|iphone|17-air|256|black|*|*",
        normalizer_version="v1",
        requested_qty=1,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    session.add(request)
    await session.flush()
    return request


@pytest.mark.asyncio
async def test_first_quote_publishes_second_does_not(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    request = await _request(db_session, seed_employee.id)
    await upsert_quote(
        db_session, request.id, seed_suppliers[0].id,
        price=Decimal("95000"), source=QuoteSource.regex, confidence=0.9,
    )
    await upsert_quote(
        db_session, request.id, seed_suppliers[1].id,
        price=Decimal("90000"), source=QuoteSource.regex, confidence=0.9,
    )

    published = (
        await db_session.scalars(
            select(RequestPriceSelection).where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status == "published",
            )
        )
    ).all()
    assert len(published) == 1
    assert published[0].client_unit_price is not None
    assert published[0].idempotency_key == first_client_price_key(request.id)

    outbox = (
        await db_session.scalars(
            select(TelegramOutbox).where(
                TelegramOutbox.dedupe_key == first_client_price_key(request.id)
            )
        )
    ).all()
    assert len(outbox) == 1
    assert "Supplier" not in outbox[0].text
    assert "95000" not in outbox[0].text
    assert "90000" not in outbox[0].text


@pytest.mark.asyncio
async def test_three_suppliers_one_client_message_all_prices_in_bot(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    mock_telegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First price reaches the client group once; later quotes stay on the card."""
    from app.services.outbox_service import process_outbox_batch
    from app.services.request_card_service import render_all_prices

    _enable(monkeypatch)
    third = Supplier(name="Supplier Three", telegram_id=200200299, active=True, dm_ok=True)
    db_session.add(third)
    await db_session.flush()
    request = await _request(db_session, seed_employee.id)
    for supplier, price in (
        (seed_suppliers[0], Decimal("95000")),
        (seed_suppliers[1], Decimal("94000")),
        (third, Decimal("96000")),
    ):
        await upsert_quote(
            db_session, request.id, supplier.id,
            price=price, source=QuoteSource.regex, confidence=0.9,
        )

    quotes = (
        await db_session.scalars(select(Quote).where(Quote.request_id == request.id))
    ).all()
    assert len(quotes) == 3

    group_rows = (
        await db_session.scalars(
            select(TelegramOutbox).where(
                TelegramOutbox.chat_id == request.group_chat_id,
                TelegramOutbox.kind == "client_price",
            )
        )
    ).all()
    assert len(group_rows) == 1

    sent = await process_outbox_batch(
        db_session, worker_id="t-three", telegram=mock_telegram
    )
    assert sent >= 1
    group_sends = [item for item in mock_telegram.sent if item[0] == request.group_chat_id]
    assert len(group_sends) == 1

    card = await render_all_prices(db_session, request.id, page=0)
    assert "Supplier One" in card
    assert "Supplier Two" in card
    assert "Supplier Three" in card
    assert "отправлена клиенту" in card


@pytest.mark.asyncio
async def test_outbox_worker_delivers_price_to_origin_group(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    mock_telegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: supplier quote -> outbox -> worker -> message in the origin group."""
    from app.services.outbox_service import process_outbox_batch

    _enable(monkeypatch)
    request = await _request(db_session, seed_employee.id)
    await upsert_quote(
        db_session, request.id, seed_suppliers[0].id,
        price=Decimal("91000"), source=QuoteSource.regex, confidence=0.9,
    )

    sent = await process_outbox_batch(
        db_session, worker_id="t-first-price", telegram=mock_telegram
    )
    assert sent == 1
    group_sends = [item for item in mock_telegram.sent if item[0] == request.group_chat_id]
    assert len(group_sends) == 1
    assert str(request.id) in group_sends[0][1]

    row = await db_session.scalar(
        select(TelegramOutbox).where(
            TelegramOutbox.dedupe_key == first_client_price_key(request.id)
        )
    )
    assert row is not None
    assert row.status == "sent"

    # A second worker pass must not resend the same price.
    again = await process_outbox_batch(
        db_session, worker_id="t-first-price", telegram=mock_telegram
    )
    assert again == 0
    group_sends = [item for item in mock_telegram.sent if item[0] == request.group_chat_id]
    assert len(group_sends) == 1


@pytest.mark.asyncio
async def test_repeated_publish_call_is_idempotent(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    request = await _request(db_session, seed_employee.id)
    await upsert_quote(
        db_session, request.id, seed_suppliers[0].id,
        price=Decimal("91000"), source=QuoteSource.regex, confidence=0.9,
    )
    again = await publish_first_valid_price(
        db_session, request_id=request.id, trigger="first_quote"
    )
    assert again is False
    count = await db_session.scalar(
        select(func.count())
        .select_from(TelegramOutbox)
        .where(TelegramOutbox.dedupe_key == first_client_price_key(request.id))
    )
    assert count == 1


@pytest.mark.asyncio
async def test_closed_request_is_not_published(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    request = await _request(db_session, seed_employee.id, status=RequestStatus.closed)
    quote = Quote(
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        price_initial=Decimal("91000"),
        source=QuoteSource.regex,
        confidence=0.9,
        available=True,
        observed_at=datetime.now(UTC),
    )
    db_session.add(quote)
    await db_session.flush()
    published = await publish_first_valid_price(
        db_session, request_id=request.id, trigger="first_quote"
    )
    assert published is False


@pytest.mark.asyncio
async def test_yesterday_quote_is_not_published(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    request = await _request(db_session, seed_employee.id)
    quote = Quote(
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        price_initial=Decimal("91000"),
        price_final=Decimal("91500"),
        source=QuoteSource.regex,
        confidence=0.9,
        available=True,
        observed_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(quote)
    await db_session.flush()
    published = await publish_first_valid_price(
        db_session, request_id=request.id, trigger="first_quote"
    )
    assert published is False


@pytest.mark.asyncio
async def test_flag_off_publishes_nothing(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    get_settings.cache_clear()
    request = await _request(db_session, seed_employee.id)
    await upsert_quote(
        db_session, request.id, seed_suppliers[0].id,
        price=Decimal("91000"), source=QuoteSource.regex, confidence=0.9,
    )
    count = await db_session.scalar(
        select(func.count())
        .select_from(RequestPriceSelection)
        .where(RequestPriceSelection.status == "published")
    )
    assert count == 0


@pytest.mark.asyncio
async def test_daily_projection_error_still_publishes_client_price(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("daily projection down")

    monkeypatch.setattr("app.services.daily_sku_price_service.on_quote_upserted", _boom)
    request = await _request(db_session, seed_employee.id)
    quote = await upsert_quote(
        db_session,
        request.id,
        seed_suppliers[0].id,
        price=Decimal("91000"),
        source=QuoteSource.regex,
        confidence=0.9,
    )
    assert quote is not None
    row = await db_session.scalar(
        select(TelegramOutbox).where(
            TelegramOutbox.request_id == request.id,
            TelegramOutbox.kind == "client_price",
        )
    )
    assert row is not None


@pytest.mark.asyncio
async def test_bare_supplier_price_reaches_client_once(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    seed_client_group,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    from app.handlers.supplier_messages import handle_reply
    from app.services.request_service import create_request
    from tests.conftest import MockTelegramClient

    telegram = MockTelegramClient()
    request, _sent = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 1TB Silver",
        telegram=telegram,
    )
    first = seed_suppliers[0]
    second = seed_suppliers[1]
    assert first.telegram_id is not None and second.telegram_id is not None
    for supplier, message_id, price_text in (
        (first, 9101, "127500"),
        (second, 9102, "120000"),
    ):
        await handle_reply(
            db_session,
            {
                "message_id": message_id,
                "chat": {"id": supplier.telegram_id, "type": "private"},
                "from": {"id": supplier.telegram_id},
                "text": price_text,
            },
            telegram=telegram,
        )

    quotes = (
        await db_session.scalars(select(Quote).where(Quote.request_id == request.id))
    ).all()
    assert len(quotes) == 2
    rows = (
        await db_session.scalars(
            select(TelegramOutbox).where(
                TelegramOutbox.request_id == request.id,
                TelegramOutbox.kind == "client_price",
                TelegramOutbox.chat_id == seed_client_group.chat_id,
            )
        )
    ).all()
    assert len(rows) == 1
    direct = [
        text
        for chat_id, text, _markup in telegram.sent
        if chat_id == seed_client_group.chat_id and "Цена:" in text
    ]
    assert direct == []
