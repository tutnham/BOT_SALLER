"""Request batch draft, preview, and confirmation lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    Request,
    RequestBatch,
    RequestBatchStatus,
    RequestPriceState,
    RequestStatus,
)
from app.db.unit_of_work import commit_or_flush
from app.llm.client import LLMProviderError, get_llm_client
from app.llm.schemas import NormalizedRequest
from app.parsers.batch_lines import BatchSourceLine, split_batch_source_text
from app.parsers.cache import get_cached, set_cached
from app.parsers.canonical_sku import (
    NORMALIZER_VERSION,
    attrs_to_normalized_json,
    build_canonical_sku_key,
    parse_batch_line,
)
from app.services.product_classifier import classify_product_deterministic

_NORMALIZE_KIND = "normalize_batch_line"


class BatchVersionConflictError(Exception):
    """Optimistic lock failed for batch edit."""


class BatchNotEditableError(Exception):
    """Batch cannot be modified in its current status."""


@dataclass(frozen=True)
class BatchPreviewStats:
    recognized: int
    needs_review: int
    duplicates: int


@dataclass(frozen=True)
class ParsedBatchLine:
    line_no: int
    source_text: str
    normalized_json: dict[str, Any]
    canonical_sku_key: str | None
    needs_review: bool


def should_use_batch_flow(source_text: str) -> bool:
    return len(split_batch_source_text(source_text)) >= 2


async def _maybe_llm_normalize_line(
    session: AsyncSession,
    source_text: str,
    deterministic: dict[str, Any],
) -> dict[str, Any]:
    settings = get_settings()
    threshold = settings.confidence_threshold
    if deterministic.get("confidence", 0.0) >= threshold:
        return deterministic

    cached = await get_cached(session, kind=_NORMALIZE_KIND, raw_text=source_text)
    if cached is not None:
        try:
            parsed = NormalizedRequest.model_validate(cached)
        except Exception:
            parsed = None
        if parsed is not None and parsed.confidence >= threshold and parsed.model.strip():
            merged = dict(deterministic)
            for field in ("model", "storage", "color", "region", "sim", "qty", "condition"):
                value = getattr(parsed, field, None)
                if value is not None and value != "":
                    merged[field] = value
            merged["confidence"] = parsed.confidence
            return merged

    llm = get_llm_client()
    try:
        parsed = await llm.normalize_request(source_text)
    except LLMProviderError:
        return deterministic

    payload = parsed.model_dump(mode="json")
    await set_cached(
        session,
        kind=_NORMALIZE_KIND,
        raw_text=source_text,
        result_json=payload,
        model_used=settings.llm_model or settings.llm_provider,
    )
    if parsed.confidence >= threshold and parsed.model.strip():
        merged = dict(deterministic)
        for field in ("model", "storage", "color", "region", "sim", "qty", "condition"):
            value = getattr(parsed, field, None)
            if value is not None and value != "":
                merged[field] = value
        merged["confidence"] = parsed.confidence
        return merged
    return deterministic


async def parse_batch_line_record(
    session: AsyncSession,
    line: BatchSourceLine,
) -> ParsedBatchLine:
    attrs = parse_batch_line(line.source_text)
    deterministic = attrs_to_normalized_json(attrs)
    normalized = await _maybe_llm_normalize_line(session, line.source_text, deterministic)
    category = classify_product_deterministic(line.source_text) or "apple"
    normalized["category"] = category
    sku_key = build_canonical_sku_key(attrs)
    needs_review = (
        normalized.get("confidence", 0.0) < get_settings().confidence_threshold
        or bool(attrs.ambiguities)
        or sku_key is None
    )
    return ParsedBatchLine(
        line_no=line.line_no,
        source_text=line.source_text,
        normalized_json=normalized,
        canonical_sku_key=sku_key,
        needs_review=needs_review,
    )


async def create_draft_batch(
    session: AsyncSession,
    *,
    group_chat_id: int,
    employee_id: int,
    source_text: str,
) -> RequestBatch:
    lines = split_batch_source_text(source_text)
    batch = RequestBatch(
        group_chat_id=group_chat_id,
        employee_id=employee_id,
        source_text=source_text,
        status=RequestBatchStatus.awaiting_confirmation,
        items_total=len(lines),
    )
    session.add(batch)
    await session.flush()

    for line in lines:
        parsed = await parse_batch_line_record(session, line)
        request = Request(
            group_chat_id=group_chat_id,
            employee_id=employee_id,
            batch_id=batch.id,
            line_no=line.line_no,
            source_line=line.source_text,
            source_text=line.source_text,
            normalized_json=parsed.normalized_json,
            canonical_sku_key=parsed.canonical_sku_key,
            normalizer_version=NORMALIZER_VERSION,
            normalization_confidence=float(parsed.normalized_json.get("confidence") or 0.0),
            requested_qty=int(parsed.normalized_json.get("qty") or 1),
            price_state=RequestPriceState.unknown,
            status=RequestStatus.open,
        )
        session.add(request)

    await session.refresh(batch, attribute_names=["requests"])
    batch.items_unresolved = sum(
        1
        for req in batch.requests
        if (req.normalization_confidence or 0.0) < get_settings().confidence_threshold
        or not req.canonical_sku_key
    )
    await session.flush()
    return batch


def compute_preview_stats(requests: list[Request]) -> BatchPreviewStats:
    seen: dict[str, int] = {}
    duplicates = 0
    needs_review = 0
    for req in requests:
        key = (req.source_line or req.source_text).strip().lower()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            duplicates += 1
        conf = req.normalization_confidence or 0.0
        if conf < get_settings().confidence_threshold or not req.canonical_sku_key:
            needs_review += 1
    return BatchPreviewStats(
        recognized=len(requests),
        needs_review=needs_review,
        duplicates=duplicates,
    )


async def load_batch_for_employee(
    session: AsyncSession,
    *,
    batch_id: int,
    employee_id: int,
    group_chat_id: int,
    is_private_chat: bool,
) -> RequestBatch | None:
    batch = await session.get(
        RequestBatch,
        batch_id,
        options=(selectinload(RequestBatch.requests),),
    )
    if batch is None:
        return None
    if is_private_chat:
        if batch.employee_id != employee_id:
            return None
    elif batch.group_chat_id != group_chat_id:
        return None
    return batch


def _assert_batch_editable(batch: RequestBatch) -> None:
    if batch.status in (
        RequestBatchStatus.published,
        RequestBatchStatus.closed,
        RequestBatchStatus.cancelled,
    ):
        raise BatchNotEditableError(batch.id)


async def confirm_batch(
    session: AsyncSession,
    *,
    batch: RequestBatch,
    expected_version: int,
    telegram: Any,
) -> RequestBatch:
    _assert_batch_editable(batch)
    if batch.version != expected_version:
        raise BatchVersionConflictError(batch.id)

    settings = get_settings()
    deadline = datetime.now(UTC) + timedelta(minutes=settings.quote_collection_window_minutes)
    for request in sorted(batch.requests, key=lambda r: r.line_no or 0):
        conf = request.normalization_confidence or 0.0
        if conf < settings.confidence_threshold:
            request.price_state = RequestPriceState.unresolved
            continue
        if not request.canonical_sku_key:
            request.price_state = RequestPriceState.unresolved
            continue
        request.quote_deadline_at = deadline
        request.price_state = RequestPriceState.collecting
        request.status = RequestStatus.open

    batch.status = RequestBatchStatus.awaiting_quotes
    batch.version += 1
    batch.updated_at = datetime.now(UTC)
    await session.flush()

    from app.services.daily_sku_price_service import apply_known_daily_prices_for_batch
    from app.services.supplier_rfq_batch_service import send_grouped_rfq_for_batch

    await apply_known_daily_prices_for_batch(session, batch=batch)
    await send_grouped_rfq_for_batch(session, batch=batch, telegram=telegram)
    await commit_or_flush(session)
    return batch


async def cancel_batch(
    session: AsyncSession,
    *,
    batch: RequestBatch,
    expected_version: int,
) -> RequestBatch:
    _assert_batch_editable(batch)
    if batch.version != expected_version:
        raise BatchVersionConflictError(batch.id)
    batch.status = RequestBatchStatus.cancelled
    batch.cancelled_at = datetime.now(UTC)
    batch.version += 1
    for request in batch.requests:
        if request.status not in (RequestStatus.closed, RequestStatus.cancelled):
            request.status = RequestStatus.cancelled
    await session.flush()
    return batch


async def list_batch_requests_ordered(session: AsyncSession, batch_id: int) -> list[Request]:
    result = await session.execute(
        select(Request)
        .where(Request.batch_id == batch_id)
        .order_by(Request.line_no.asc())
    )
    return list(result.scalars().all())
