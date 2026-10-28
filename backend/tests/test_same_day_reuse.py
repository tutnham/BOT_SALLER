"""Same-day reuse gates: an existing daily price is reusable only while valid today."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Quote,
    QuoteSource,
    Request,
    RequestPriceState,
    RequestStatus,
    Supplier,
)
from app.parsers.canonical_sku import build_canonical_sku_key, parse_batch_line
from app.services.daily_sku_price_service import (
    get_fresh_daily_price,
    upsert_daily_from_quote,
)
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient

SKU = "v1|apple|iphone|14|128|yellow|*|*"
LINE = "14 128GB Yellow iPhone"


async def _seed(
    session: AsyncSession,
    *,
    employee_id: int,
    supplier: Supplier,
    price: Decimal = Decimal("92000"),
    sku: str | None = None,
    confidence: float = 0.9,
    available: bool | None = True,
    observed_at: datetime | None = None,
) -> tuple[Request, Quote]:
    sku_key = sku or SKU
    request = Request(
        group_chat_id=-1001,
        employee_id=employee_id,
        source_text=LINE,
        status=RequestStatus.awaiting_answers,
        canonical_sku_key=sku_key,
        normalizer_version="v1",
        requested_qty=1,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    session.add(request)
    await session.flush()
    quote = Quote(
        request_id=request.id,
        supplier_id=supplier.id,
        price_initial=price,
        source=QuoteSource.regex,
        confidence=confidence,
        available=available,
        price_final=price + Decimal("500"),
        observed_at=observed_at or datetime.now(UTC),
    )
    session.add(quote)
    await session.flush()
    await upsert_daily_from_quote(session, quote=quote, request=request)
    return request, quote


@pytest.mark.asyncio
async def test_inactive_supplier_price_not_reused(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    await _seed(
        db_session,
        employee_id=seed_employee.id,
        supplier=seed_suppliers[2],  # inactive
    )
    assert await get_fresh_daily_price(db_session, canonical_sku_key=SKU) is None


@pytest.mark.asyncio
async def test_low_confidence_price_not_reused(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    await _seed(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0], confidence=0.1
    )
    assert await get_fresh_daily_price(db_session, canonical_sku_key=SKU) is None


@pytest.mark.asyncio
async def test_unavailable_price_not_reused(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    await _seed(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0], available=False
    )
    assert await get_fresh_daily_price(db_session, canonical_sku_key=SKU) is None


@pytest.mark.asyncio
async def test_quote_observed_yesterday_not_reused(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    _request, quote = await _seed(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0]
    )
    quote.observed_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()
    assert await get_fresh_daily_price(db_session, canonical_sku_key=SKU) is None


@pytest.mark.asyncio
async def test_single_ask_reuses_today_price_without_rfq(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    await _seed(db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0])
    telegram = MockTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text=LINE,
        telegram=telegram,
    )
    request = outcome.requests[0]
    assert request.canonical_sku_key == build_canonical_sku_key(
        parse_batch_line(LINE)
    )
    assert request.price_state == RequestPriceState.known_today
    assert outcome.sent_count == 0
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_single_ask_without_today_price_sends_rfq(
    db_session: AsyncSession, seed_employee, seed_suppliers: list[Supplier], seed_markup_rules
) -> None:
    telegram = MockTelegramClient()
    outcome = await create_request(
        db_session,
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text=LINE,
        telegram=telegram,
    )
    assert outcome.requests[0].price_state != RequestPriceState.known_today
    assert outcome.sent_count >= 1
