from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Deal,
    PriceSelectionStatus,
    Quote,
    QuoteSource,
    Request,
    RequestBatch,
    RequestBatchStatus,
    RequestPriceSelection,
    RequestStatus,
    Supplier,
)
from app.services.deal_service import DealAlreadyExistsError
from app.services.purchase_confirmation_service import (
    confirm_batch_purchases,
    confirm_purchase_for_request,
)


@pytest.mark.asyncio
async def test_confirm_creates_one_deal_and_is_idempotent(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
    seed_suppliers: list[Supplier],
) -> None:
    batch = RequestBatch(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="17 Air 256GB Black",
        status=RequestBatchStatus.published.value,
        items_total=1,
    )
    db_session.add(batch)
    await db_session.flush()
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        batch_id=batch.id,
        line_no=1,
        source_text="17 Air 256GB Black",
        status=RequestStatus.priced,
        requested_qty=1,
    )
    db_session.add(request)
    await db_session.flush()
    quote = Quote(
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        price_initial=Decimal("92000"),
        source=QuoteSource.regex,
        confidence=0.9,
        available=True,
    )
    db_session.add(quote)
    await db_session.flush()
    selection = RequestPriceSelection(
        request_id=request.id,
        batch_id=batch.id,
        status=PriceSelectionStatus.published.value,
        selected_quote_id=quote.id,
        selected_supplier_id=seed_suppliers[0].id,
        purchase_unit_price=Decimal("92000"),
        requested_qty=1,
        client_unit_price=Decimal("92500"),
        selection_reason="lowest",
        candidate_count=1,
        selection_version=1,
    )
    db_session.add(selection)
    await db_session.flush()

    deal = await confirm_purchase_for_request(
        db_session,
        request=request,
        supplier_id=seed_suppliers[0].id,
        purchase_unit_price=Decimal("92000"),
        employee_id=seed_employee.id,
        client_unit_price=Decimal("92500"),
        source_quote_id=quote.id,
        selection_id=selection.id,
    )
    assert deal.id is not None
    with pytest.raises(DealAlreadyExistsError):
        await confirm_purchase_for_request(
            db_session,
            request=request,
            supplier_id=seed_suppliers[1].id,
            purchase_unit_price=Decimal("91000"),
            employee_id=seed_employee.id,
            override_reason="other supplier",
        )
    count = await db_session.scalar(select(func.count()).select_from(Deal))
    assert count == 1
    assert selection.selected_supplier_id == seed_suppliers[0].id


@pytest.mark.asyncio
async def test_batch_confirm_skips_existing(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
    seed_suppliers: list[Supplier],
) -> None:
    batch = RequestBatch(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="x",
        status=RequestBatchStatus.published.value,
        items_total=0,
    )
    db_session.add(batch)
    await db_session.flush()
    await db_session.refresh(batch, attribute_names=["requests"])
    deals = await confirm_batch_purchases(
        db_session, batch=batch, employee_id=seed_employee.id
    )
    assert deals == []
