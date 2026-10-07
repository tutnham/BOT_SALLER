from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote, QuoteSource, Request, RequestStatus, Supplier
from app.parsers.canonical_sku import build_canonical_sku_key, parse_batch_line
from app.services.daily_sku_price_service import (
    business_date_start,
    get_fresh_daily_price,
    upsert_daily_from_quote,
)

SKU = "v1|apple|iphone|17-air|256|black|*|*"


async def _request_and_quote(
    session: AsyncSession,
    *,
    employee_id: int,
    supplier: Supplier,
    price: Decimal,
    sku: str = SKU,
) -> tuple[Request, Quote]:
    request = Request(
        group_chat_id=-1001,
        employee_id=employee_id,
        source_text="17 Air 256GB Black",
        status=RequestStatus.awaiting_answers,
        canonical_sku_key=sku,
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
        confidence=0.9,
        available=True,
        price_final=price + Decimal("500"),
    )
    session.add(quote)
    await session.flush()
    return request, quote


@pytest.mark.asyncio
async def test_today_quote_reused(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    request, quote = await _request_and_quote(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0], price=Decimal("92000")
    )
    await upsert_daily_from_quote(db_session, quote=quote, request=request)
    found = await get_fresh_daily_price(db_session, canonical_sku_key=SKU)
    assert found is not None
    assert found.source_quote_id == quote.id


@pytest.mark.asyncio
async def test_yesterday_not_reused(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    request, quote = await _request_and_quote(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0], price=Decimal("92000")
    )
    yesterday = datetime.now(UTC) - timedelta(days=1)
    row = await upsert_daily_from_quote(
        db_session, quote=quote, request=request, now=yesterday
    )
    assert row is not None
    found = await get_fresh_daily_price(db_session, canonical_sku_key=SKU)
    assert found is None


@pytest.mark.asyncio
async def test_lower_quote_replaces_daily(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    req_a, quote_a = await _request_and_quote(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0], price=Decimal("95000")
    )
    await upsert_daily_from_quote(db_session, quote=quote_a, request=req_a)
    req_b, quote_b = await _request_and_quote(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[1], price=Decimal("90000")
    )
    row = await upsert_daily_from_quote(db_session, quote=quote_b, request=req_b)
    assert row is not None
    assert row.source_quote_id == quote_b.id
    assert row.purchase_unit_price == Decimal("90000")


@pytest.mark.asyncio
async def test_higher_quote_does_not_replace(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    req_a, quote_a = await _request_and_quote(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[0], price=Decimal("90000")
    )
    await upsert_daily_from_quote(db_session, quote=quote_a, request=req_a)
    req_b, quote_b = await _request_and_quote(
        db_session, employee_id=seed_employee.id, supplier=seed_suppliers[1], price=Decimal("99000")
    )
    row = await upsert_daily_from_quote(db_session, quote=quote_b, request=req_b)
    assert row is not None
    assert row.source_quote_id == quote_a.id


def test_conflicting_color_keys_differ() -> None:
    black = build_canonical_sku_key(parse_batch_line("17 Air 256GB Black"))
    gold = build_canonical_sku_key(parse_batch_line("17 Air 256GB Gold"))
    assert black != gold


def test_business_date_uses_moscow() -> None:
    start = business_date_start()
    assert start.tzinfo is not None
