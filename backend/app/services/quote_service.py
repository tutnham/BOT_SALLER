"""Quote upsert with confidence gate (TECH DOC §8.3, §9.2)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Quote, QuoteSource, Request, RequestStatus


async def select_best_quote(
    session: AsyncSession,
    request_id: int,
    *,
    prefer_bargain: bool = False,
) -> Quote | None:
    """
    Best quote for a request.

    When ``prefer_bargain`` is True: min(COALESCE(price_bargain, price_initial)).
    Otherwise: min(price_initial). Tie-break: lowest quote.id.
    """
    if prefer_bargain:
        effective = func.coalesce(Quote.price_bargain, Quote.price_initial)
        stmt = (
            select(Quote)
            .where(
                Quote.request_id == request_id,
                effective.is_not(None),
            )
            .options(selectinload(Quote.supplier))
            .order_by(effective.asc(), Quote.id.asc())
            .limit(1)
        )
    else:
        stmt = (
            select(Quote)
            .where(
                Quote.request_id == request_id,
                Quote.price_initial.is_not(None),
            )
            .options(selectinload(Quote.supplier))
            .order_by(Quote.price_initial.asc(), Quote.id.asc())
            .limit(1)
        )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_quote(
    session: AsyncSession,
    request_id: int,
    supplier_id: int,
    *,
    price: Decimal | None = None,
    qty: int | None = None,
    available: bool | None = None,
    condition: str | None = None,
    source: QuoteSource,
    confidence: float | None = None,
    bargain: bool = False,
) -> Quote | None:
    """
    Upsert quote by (request_id, supplier_id).

    When ``bargain`` is True and price is present, write ``price_bargain``
    and leave ``price_initial`` unchanged (TECH DOC §9.3).

    Returns None when confidence gate rejects non-manual writes.
    """
    settings = get_settings()
    effective_confidence = confidence if confidence is not None else 1.0

    if source != QuoteSource.manual and effective_confidence < settings.confidence_threshold:
        return None

    if source == QuoteSource.manual and confidence is None:
        effective_confidence = 1.0

    values: dict[str, object] = {
        "request_id": request_id,
        "supplier_id": supplier_id,
        "source": source,
        "confidence": effective_confidence,
    }
    if price is not None:
        values["price_bargain" if bargain else "price_initial"] = price
    if qty is not None:
        values["qty"] = qty
    if available is not None:
        values["available"] = available
    if condition is not None:
        values["condition"] = condition

    update_values: dict[str, object] = {
        "source": source,
        "confidence": effective_confidence,
    }
    if price is not None:
        update_values["price_bargain" if bargain else "price_initial"] = price
    if qty is not None:
        update_values["qty"] = qty
    if available is not None:
        update_values["available"] = available
    if condition is not None:
        update_values["condition"] = condition

    stmt = (
        insert(Quote)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["request_id", "supplier_id"],
            set_=update_values,
        )
        .returning(Quote.id)
    )
    quote_id = (await session.execute(stmt)).scalar_one()
    quote = await session.get(Quote, quote_id)
    assert quote is not None

    await _maybe_transition_to_priced(session, request_id)
    return quote


async def _maybe_transition_to_priced(session: AsyncSession, request_id: int) -> None:
    """Set request status to priced on first quote, from open/awaiting_answers only."""
    result = await session.execute(select(Request).where(Request.id == request_id))
    request = result.scalar_one_or_none()
    if request is None:
        return

    if request.status not in (RequestStatus.open, RequestStatus.awaiting_answers):
        return

    count_result = await session.execute(
        select(func.count()).select_from(Quote).where(Quote.request_id == request_id)
    )
    quote_count = count_result.scalar_one()
    if quote_count >= 1 and any(
        quote.price_initial is not None for quote in await _fetch_request_quotes(session, request_id)
    ):
        request.status = RequestStatus.priced
        await session.flush()


async def _fetch_request_quotes(session: AsyncSession, request_id: int) -> list[Quote]:
    result = await session.execute(select(Quote).where(Quote.request_id == request_id))
    return list(result.scalars().all())
