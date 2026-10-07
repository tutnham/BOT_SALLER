"""Grouped supplier RFQ delivery for request batches."""

from __future__ import annotations

from collections import defaultdict

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    MessageKind,
    Request,
    RequestBatch,
    RequestPriceState,
    RequestStatus,
    RfqItemDeliveryStatus,
    SupplierRfqBatch,
    SupplierRfqBatchItem,
)
from app.services.routing_service import resolve_rfq_targets
from app.services.supplier_delivery import deliver, is_sent
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template


def _line_code(batch_id: int, line_no: int) -> str:
    return f"#{batch_id}-{line_no:02d}"


def _chunk_lines(
    batch_id: int,
    items: list[Request],
    *,
    max_len: int,
) -> list[list[Request]]:
    chunks: list[list[Request]] = []
    current: list[Request] = []
    current_len = 0
    header_reserve = 120

    for request in items:
        line_no = request.line_no or 0
        line_text = f"{_line_code(batch_id, line_no)} {request.source_text}\n"
        if current and current_len + len(line_text) + header_reserve > max_len:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(request)
        current_len += len(line_text)
    if current:
        chunks.append(current)
    return chunks


async def send_grouped_rfq_for_batch(
    session: AsyncSession,
    *,
    batch: RequestBatch,
    telegram: TelegramClientProtocol,
) -> int:
    settings = get_settings()
    max_len = settings.max_message_text_len
    to_send = [
        r
        for r in sorted(batch.requests, key=lambda x: x.line_no or 0)
        if r.price_state
        in (
            RequestPriceState.collecting.value,
            RequestPriceState.unknown.value,
            None,
        )
        and r.status not in (RequestStatus.cancelled, RequestStatus.closed)
        and (r.normalization_confidence or 0.0) >= settings.confidence_threshold
        and r.canonical_sku_key
    ]
    if not to_send:
        return 0

    by_category: dict[str, list[Request]] = defaultdict(list)
    for request in to_send:
        category = str((request.normalized_json or {}).get("category") or "apple")
        by_category[category].append(request)

    sent_messages = 0
    for category, category_requests in by_category.items():
        targets, _skipped = await resolve_rfq_targets(session, category=category)
        if not targets:
            continue
        for target in targets:
            chunks = _chunk_lines(batch.id, category_requests, max_len=max_len)
            for chunk_no, chunk in enumerate(chunks, start=1):
                rfq_batch = SupplierRfqBatch(
                    batch_id=batch.id,
                    supplier_id=target.supplier.id,
                    business_connection_id=target.business_connection_id,
                    chat_id=target.chat_id,
                    chunk_no=chunk_no,
                )
                session.add(rfq_batch)
                await session.flush()

                body_lines = [
                    f"{_line_code(batch.id, req.line_no or 0)} {req.source_text}"
                    for req in chunk
                ]
                text = render_template(
                    "batch_ask_supplier",
                    batch_id=batch.id,
                    lines_block="\n".join(body_lines),
                )
                outbound = await deliver(
                    session,
                    telegram,
                    supplier_id=target.supplier.id,
                    chat_id=target.chat_id,
                    text=text,
                    kind=MessageKind.ask,
                    request_id=None,
                    business_connection_id=target.business_connection_id,
                )
                delivery = (
                    RfqItemDeliveryStatus.sent
                    if is_sent(outbound)
                    else RfqItemDeliveryStatus.failed
                )
                for req in chunk:
                    item = SupplierRfqBatchItem(
                        rfq_batch_id=rfq_batch.id,
                        request_id=req.id,
                        line_no=req.line_no or 0,
                        line_code=_line_code(batch.id, req.line_no or 0),
                        message_out_id=outbound.id,
                        delivery_status=delivery.value,
                    )
                    session.add(item)
                    if delivery == RfqItemDeliveryStatus.sent:
                        req.status = RequestStatus.awaiting_answers
                if is_sent(outbound):
                    sent_messages += 1
                else:
                    logger.warning(
                        "batch RFQ failed batch_id={} supplier_id={}",
                        batch.id,
                        target.supplier.id,
                    )
    await session.flush()
    return sent_messages


async def find_request_by_line_code(
    session: AsyncSession,
    line_code: str,
) -> Request | None:
    normalized = line_code.strip().lstrip("#")
    item = await session.scalar(
        select(SupplierRfqBatchItem)
        .where(SupplierRfqBatchItem.line_code.in_({line_code, f"#{normalized}"}))
        .options(selectinload(SupplierRfqBatchItem.request))
        .limit(1)
    )
    if item is not None and item.request is not None:
        return item.request
    return None


async def requests_for_message_out(
    session: AsyncSession,
    message_out_id: int,
) -> list[Request]:
    result = await session.execute(
        select(Request)
        .join(SupplierRfqBatchItem, SupplierRfqBatchItem.request_id == Request.id)
        .where(SupplierRfqBatchItem.message_out_id == message_out_id)
        .order_by(Request.line_no.asc())
    )
    return list(result.scalars().all())
