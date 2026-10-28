"""Publish the first valid same-day price to the client exactly once.

Uniqueness is guaranteed by the database, not by in-process locks:
``request_price_selections.idempotency_key`` (partial unique) and
``telegram_outbox.dedupe_key`` (unique). The client message is only enqueued
here; delivery happens in the existing outbox worker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    PriceSelectionStatus,
    Quote,
    Request,
    RequestPriceSelection,
    RequestPriceState,
    RequestStatus,
    TelegramOutbox,
    TelegramOutboxStatus,
)
from app.services.best_quote_service import select_best_quote_for_request
from app.templates.messages_ru import render_template
from app.utils.business_time import get_business_day_bounds

FIRST_PRICE_KEY = "request:{request_id}:first-client-price"
REASON_FIRST_QUOTE = "first_valid_today"
REASON_KNOWN_TODAY = "known_today"


def first_client_price_key(request_id: int) -> str:
    return FIRST_PRICE_KEY.format(request_id=request_id)


async def publish_first_valid_price(
    session: AsyncSession,
    *,
    request_id: int,
    trigger: str,
    now: datetime | None = None,
) -> bool:
    """Publish the lowest eligible same-day price. Returns True when newly published.

    ``trigger`` is ``first_quote`` or ``known_today``. A closed request, a missing
    route, or an already published price all result in a silent no-op.
    """
    settings = get_settings()
    if not settings.auto_publish_first_valid_price:
        return False

    moment = now or datetime.now(UTC)
    request = await session.get(Request, request_id)
    if request is None or request.status in (RequestStatus.closed, RequestStatus.cancelled):
        return False

    result = await select_best_quote_for_request(session, request_id, now=moment)
    if result.selected_quote_id is None or result.client_unit_price is None:
        return False

    quote = await session.get(Quote, result.selected_quote_id)
    if quote is None or not _observed_today(quote, moment, settings.tz):
        return False

    key = first_client_price_key(request_id)
    text = render_template(
        "supplier_quote_parsed", request_id=request_id, price=result.client_unit_price
    )
    inserted_id = await _insert_published_selection(
        session,
        request=request,
        result_quote_id=quote.id,
        result_supplier_id=result.selected_supplier_id,
        purchase=result.purchase_unit_price,
        qty=result.requested_qty,
        client_price=result.client_unit_price,
        candidate_count=result.candidate_count,
        rejected=result.rejected_candidate_reasons,
        key=key,
        reason=REASON_KNOWN_TODAY if trigger == "known_today" else REASON_FIRST_QUOTE,
        moment=moment,
    )
    if inserted_id is None:
        logger.info("client price already published request_id={}", request_id)
        from app.services.telemetry import record_duplicate_publication_prevented

        record_duplicate_publication_prevented()
        return False

    outbox_id = await _enqueue_client_message(
        session, dedupe_key=key, chat_id=request.group_chat_id, text=text, request_id=request_id
    )
    selection = await session.get(RequestPriceSelection, inserted_id)
    if selection is not None:
        selection.telegram_outbox_id = outbox_id
    request.price_state = RequestPriceState.published
    await session.flush()
    from app.services.telemetry import record_client_price_publication

    record_client_price_publication(
        REASON_KNOWN_TODAY if trigger == "known_today" else REASON_FIRST_QUOTE
    )
    return True


async def _enqueue_client_message(
    session: AsyncSession,
    *,
    dedupe_key: str,
    chat_id: int,
    text: str,
    request_id: int,
    reply_markup: dict[str, object] | None = None,
) -> int | None:
    """Queue a client message directly.

    ``messages_out`` requires a supplier, and a client price has none, so the
    row goes straight into ``telegram_outbox``. The unique ``dedupe_key`` is the
    second line of duplicate protection.
    """
    inserted = (
        await session.execute(
            pg_insert(TelegramOutbox)
            .values(
                dedupe_key=dedupe_key,
                chat_id=chat_id,
                request_id=request_id,
                kind="client_price",
                text=text,
                reply_markup=reply_markup,
                status=TelegramOutboxStatus.pending.value,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(TelegramOutbox.id)
        )
    ).scalar_one_or_none()
    return inserted


def _observed_today(quote: Quote, moment: datetime, tz_name: str) -> bool:
    observed = quote.observed_at or quote.created_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    day_start, next_day_start = get_business_day_bounds(moment, tz_name)
    return day_start <= observed < next_day_start


async def _insert_published_selection(
    session: AsyncSession,
    *,
    request: Request,
    result_quote_id: int,
    result_supplier_id: int | None,
    purchase: Decimal | None,
    qty: int | None,
    client_price: Decimal | None,
    candidate_count: int,
    rejected: dict[str, str],
    key: str,
    reason: str,
    moment: datetime,
) -> int | None:
    """Insert the published row; return its id, or None when the key already exists."""
    version = await _next_selection_version(session, request.id)
    stmt = (
        pg_insert(RequestPriceSelection)
        .values(
            request_id=request.id,
            batch_id=request.batch_id,
            canonical_sku_key=request.canonical_sku_key,
            status=PriceSelectionStatus.published.value,
            selected_quote_id=result_quote_id,
            selected_supplier_id=result_supplier_id,
            purchase_unit_price=purchase,
            requested_qty=qty,
            client_unit_price=client_price,
            selection_reason="lowest_eligible_purchase_price",
            candidate_count=candidate_count,
            rejected_candidates=rejected,
            selection_version=version,
            selected_at=moment,
            idempotency_key=key,
            published_at=moment,
            publication_reason=reason,
        )
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            index_where=RequestPriceSelection.idempotency_key.is_not(None),
        )
        .returning(RequestPriceSelection.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _next_selection_version(session: AsyncSession, request_id: int) -> int:
    current = await session.scalar(
        select(RequestPriceSelection.selection_version)
        .where(RequestPriceSelection.request_id == request_id)
        .order_by(RequestPriceSelection.selection_version.desc())
        .limit(1)
    )
    return (current or 0) + 1
