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
