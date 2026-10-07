from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, Quote, QuoteSource, Request, RequestStatus, Supplier
from app.services.best_quote_service import select_best_quote_for_request
from app.services.quote_eligibility import is_quote_eligible_for_selection


@pytest.mark.asyncio
async def test_lowest_price_wins(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee,
    seed_markup_rules,
) -> None:
    request = Request(
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text="17 Air 256GB Black",
        normalized_json={"model": "17 Air 256GB Black", "qty": 1},
        status=RequestStatus.awaiting_answers,
        canonical_sku_key="v1|apple|iphone|17-air|256|black|*|*",
        requested_qty=1,
    )
    db_session.add(request)
    await db_session.flush()

    prices = (
        (seed_suppliers[0].id, Decimal("95000")),
        (seed_suppliers[1].id, Decimal("92000")),
    )
    for supplier_id, price in prices:
        db_session.add(
            Quote(
                request_id=request.id,
                supplier_id=supplier_id,
                price_initial=price,
                source=QuoteSource.regex,
                confidence=0.9,
                available=True,
            )
        )
    await db_session.flush()

    result = await select_best_quote_for_request(db_session, request.id)
    assert result.selected_quote_id is not None
    assert result.purchase_unit_price == Decimal("92000")
    assert result.client_unit_price is not None
    deal_count = await db_session.scalar(select(func.count()).select_from(Deal))
    assert deal_count == 0


@pytest.mark.asyncio
async def test_unavailable_quote_rejected(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee,
) -> None:
    seed_supplier = seed_suppliers[0]
    request = Request(
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text="test",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    quote = Quote(
        request_id=request.id,
        supplier_id=seed_supplier.id,
        price_initial=Decimal("50000"),
        source=QuoteSource.regex,
        confidence=0.9,
        available=False,
    )
    db_session.add(quote)
    await db_session.flush()
    ok, reason = is_quote_eligible_for_selection(
        quote, request, seed_supplier
    )
    assert not ok
    assert reason == "not_available"
