"""Employee-confirmed purchases for batch items."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Deal,
    PriceSelectionStatus,
    Request,
    RequestBatch,
    RequestBatchStatus,
    RequestPriceSelection,
    RequestStatus,
)
from app.services.deal_service import DealAlreadyExistsError, create_deal


async def confirm_purchase_for_request(
    session: AsyncSession,
    *,
    request: Request,
    supplier_id: int,
    purchase_unit_price: Decimal,
    employee_id: int,
    purchased_qty: int | None = None,
    client_unit_price: Decimal | None = None,
    override_reason: str | None = None,
    selection_id: int | None = None,
    source_quote_id: int | None = None,
) -> Deal:
    existing = await session.scalar(
        select(Deal.id).where(Deal.request_id == request.id).limit(1)
    )
    if existing is not None:
        raise DealAlreadyExistsError(request.id)

    deal = await create_deal(
        session,
        request_id=request.id,
        supplier_id=supplier_id,
        final_price=purchase_unit_price,
    )
    deal.purchased_qty = purchased_qty or request.requested_qty or 1
    deal.client_unit_price = client_unit_price
    deal.override_reason = override_reason
    deal.recorded_by_employee_id = employee_id
    deal.selection_id = selection_id
    deal.source_quote_id = source_quote_id
    await session.flush()
    return deal


async def confirm_batch_purchases(
    session: AsyncSession,
    *,
    batch: RequestBatch,
    employee_id: int,
) -> list[Deal]:
    deals: list[Deal] = []
    for request in batch.requests:
        if request.status in (RequestStatus.cancelled, RequestStatus.closed):
            continue
        selection = await session.scalar(
            select(RequestPriceSelection)
            .where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status.in_(
                    (
                        PriceSelectionStatus.final.value,
                        PriceSelectionStatus.published.value,
                        PriceSelectionStatus.provisional.value,
                    )
                ),
            )
            .order_by(RequestPriceSelection.selection_version.desc())
            .limit(1)
        )
        if selection is None or selection.selected_supplier_id is None:
            continue
        if selection.purchase_unit_price is None:
            continue
        try:
            deal = await confirm_purchase_for_request(
                session,
                request=request,
                supplier_id=selection.selected_supplier_id,
                purchase_unit_price=selection.purchase_unit_price,
                employee_id=employee_id,
                purchased_qty=selection.requested_qty,
                client_unit_price=selection.client_unit_price,
                selection_id=selection.id,
                source_quote_id=selection.selected_quote_id,
            )
        except DealAlreadyExistsError:
            continue
        deals.append(deal)
    batch.status = RequestBatchStatus.closed
    batch.updated_at = datetime.now(UTC)
    await session.flush()
    return deals
