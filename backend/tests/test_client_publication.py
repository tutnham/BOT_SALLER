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
from app.services.client_publication_service import publish_batch_to_client
from tests.conftest import MockTelegramClient


@pytest.mark.asyncio
async def test_client_publication_hides_supplier_and_is_idempotent(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
) -> None:
    batch = RequestBatch(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="17 Air 256GB Black",
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
        source_text="17 Air 256GB Black",
        status=RequestStatus.priced,
        canonical_sku_key="v1|apple|iphone|17-air|256|black|*|*",
        requested_qty=1,
        price_state=RequestPriceState.finalized.value,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    db_session.add(request)
    await db_session.flush()
    quote = Quote(
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        price_initial=Decimal("92000"),
        source=QuoteSource.regex,
        confidence=0.95,
        available=True,
        price_final=Decimal("92500"),
    )
    db_session.add(quote)
    await db_session.flush()
    db_session.add(
        RequestPriceSelection(
            request_id=request.id,
            batch_id=batch.id,
            canonical_sku_key=request.canonical_sku_key,
            status=PriceSelectionStatus.final.value,
            selected_quote_id=quote.id,
            selected_supplier_id=seed_suppliers[0].id,
            purchase_unit_price=Decimal("92000"),
            requested_qty=1,
            client_unit_price=Decimal("92500"),
            selection_reason="lowest_eligible_purchase_price",
            candidate_count=1,
            selection_version=1,
        )
    )
    await db_session.flush()
    await db_session.refresh(batch, attribute_names=["requests"])

    telegram = MockTelegramClient()
    first = await publish_batch_to_client(db_session, batch=batch, telegram=telegram)
    second = await publish_batch_to_client(db_session, batch=batch, telegram=telegram)
    assert first >= 1
    assert second == 0
    body = "\n".join(text for _chat, text, _m in telegram.sent)
    assert "92000" not in body
    assert seed_suppliers[0].name not in body
    assert "92500" in body
    assert "Поставщик" not in body
