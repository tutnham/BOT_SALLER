"""Consolidated client price publication for batches."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    PriceSelectionStatus,
    Request,
    RequestBatch,
    RequestBatchStatus,
    RequestPriceSelection,
    RequestPriceState,
)
from app.services.best_quote_service import select_best_quote_for_request
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template


def _client_price_line(request: Request, selection: RequestPriceSelection | None) -> str:
    qty = request.requested_qty or int((request.normalized_json or {}).get("qty") or 1)
    product = request.source_text
    if selection is not None and selection.client_unit_price is not None:
        price_text = f"{selection.client_unit_price} ₽"
    elif request.price_state == RequestPriceState.unresolved:
        price_text = "Цена уточняется"
    else:
        price_text = "Цена уточняется"
    return render_template(
        "batch_client_line",
        product=product,
        qty=qty,
        price_text=price_text,
    )


async def finalize_batch_selections(
    session: AsyncSession,
    *,
    batch: RequestBatch,
) -> None:
    for request in batch.requests:
        if request.price_state == RequestPriceState.published:
            continue
        result = await select_best_quote_for_request(session, request.id)
        if result.selected_quote_id is None:
            request.price_state = RequestPriceState.unresolved
            continue
        published = await session.scalar(
            select(RequestPriceSelection)
            .where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status == PriceSelectionStatus.published.value,
            )
            .limit(1)
        )
        if published is not None:
            continue
        existing_final = await session.scalar(
            select(RequestPriceSelection)
            .where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status == PriceSelectionStatus.final.value,
            )
            .order_by(RequestPriceSelection.selection_version.desc())
            .limit(1)
        )
        if existing_final is not None:
            request.price_state = RequestPriceState.finalized.value
            continue
        provisional = await session.scalar(
            select(RequestPriceSelection)
            .where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status == PriceSelectionStatus.provisional.value,
            )
            .order_by(RequestPriceSelection.selection_version.desc())
            .limit(1)
        )
        if provisional is not None:
            provisional.status = PriceSelectionStatus.final.value
            request.price_state = RequestPriceState.finalized.value
            continue
        session.add(
            RequestPriceSelection(
                request_id=request.id,
                batch_id=batch.id,
                canonical_sku_key=result.canonical_sku_key,
                status=PriceSelectionStatus.final.value,
                selected_quote_id=result.selected_quote_id,
                selected_supplier_id=result.selected_supplier_id,
                purchase_unit_price=result.purchase_unit_price,
                requested_qty=result.requested_qty,
                client_unit_price=result.client_unit_price,
                selection_reason=result.selection_reason,
                candidate_count=result.candidate_count,
                rejected_candidates=result.rejected_candidate_reasons,
                selection_version=1,
                selected_at=result.selected_at or datetime.now(UTC),
            )
        )
        request.price_state = RequestPriceState.finalized.value
    batch.finalized_at = datetime.now(UTC)
    await session.flush()


async def publish_batch_to_client(
    session: AsyncSession,
    *,
    batch: RequestBatch,
    telegram: TelegramClientProtocol,
) -> int:
    if batch.status == RequestBatchStatus.published:
        return 0
    settings = get_settings()
    await finalize_batch_selections(session, batch=batch)
    batch.selection_version += 1

    lines: list[str] = []
    for request in sorted(batch.requests, key=lambda r: r.line_no or 0):
        selection = await session.scalar(
            select(RequestPriceSelection)
            .where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status == PriceSelectionStatus.final.value,
            )
            .order_by(RequestPriceSelection.selection_version.desc())
            .limit(1)
        )
        lines.append(_client_price_line(request, selection))
        if selection is not None:
            selection.status = PriceSelectionStatus.published.value
            request.price_state = RequestPriceState.published

    max_len = settings.max_message_text_len
    parts: list[str] = []
    current = render_template("batch_client_header", batch_id=batch.id) + "\n"
    for line in lines:
        if len(current) + len(line) + 1 > max_len and current.strip():
            parts.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        parts.append(current.rstrip())

    sent = 0
    for _part_no, text in enumerate(parts, start=1):
        await telegram.send_message(batch.group_chat_id, text)
        sent += 1

    batch.status = RequestBatchStatus.published
    batch.published_at = datetime.now(UTC)
    await session.flush()
    return sent
