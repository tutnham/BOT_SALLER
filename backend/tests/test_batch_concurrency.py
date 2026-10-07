from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    PriceSelectionStatus,
    Quote,
    QuoteSource,
    Request,
    RequestBatch,
    RequestBatchStatus,
    RequestPriceSelection,
    RequestPriceState,
    RequestStatus,
    Supplier,
)
from app.services.best_quote_service import select_best_quote_for_request
from app.services.client_publication_service import publish_batch_to_client
from tests.conftest import MockTelegramClient


@pytest.mark.asyncio
async def test_two_quotes_one_deterministic_winner(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    request = Request(
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text="17 Air 256GB Black",
        status=RequestStatus.awaiting_answers,
        canonical_sku_key="v1|apple|iphone|17-air|256|black|*|*",
        requested_qty=1,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    db_session.add(request)
    await db_session.flush()
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=seed_suppliers[0].id,
            price_initial=Decimal("93000"),
            source=QuoteSource.regex,
            confidence=0.9,
            available=True,
        )
    )
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=seed_suppliers[1].id,
            price_initial=Decimal("91000"),
            source=QuoteSource.regex,
            confidence=0.9,
            available=True,
        )
    )
    await db_session.flush()
    first = await select_best_quote_for_request(db_session, request.id)
    second = await select_best_quote_for_request(db_session, request.id)
    assert first.selected_quote_id == second.selected_quote_id
    assert first.purchase_unit_price == Decimal("91000")


@pytest.mark.asyncio
async def test_double_publish_one_client_message(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    batch = RequestBatch(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="14 128GB Yellow",
        status=RequestBatchStatus.ready.value,
        items_total=1,
    )
    db_session.add(batch)
    await db_session.flush()
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        batch_id=batch.id,
        line_no=1,
        source_text="14 128GB Yellow",
        status=RequestStatus.priced,
        requested_qty=1,
        price_state=RequestPriceState.finalized.value,
        canonical_sku_key="v1|apple|iphone|14|128|yellow|*|*",
        normalized_json={"model": "14", "qty": 1},
    )
    db_session.add(request)
    await db_session.flush()
    quote = Quote(
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        price_initial=Decimal("50000"),
        source=QuoteSource.regex,
        confidence=0.95,
        available=True,
        price_final=Decimal("50500"),
    )
    db_session.add(quote)
    await db_session.flush()
    db_session.add(
        RequestPriceSelection(
            request_id=request.id,
            batch_id=batch.id,
            status=PriceSelectionStatus.final.value,
            selected_quote_id=quote.id,
            selected_supplier_id=seed_suppliers[0].id,
            purchase_unit_price=Decimal("50000"),
            client_unit_price=Decimal("50500"),
            requested_qty=1,
            selection_reason="lowest",
            candidate_count=1,
            selection_version=1,
        )
    )
    await db_session.flush()
    await db_session.refresh(batch, attribute_names=["requests"])
    telegram = MockTelegramClient()
    await publish_batch_to_client(db_session, batch=batch, telegram=telegram)
    await publish_batch_to_client(db_session, batch=batch, telegram=telegram)
    assert len(telegram.sent) == 1
