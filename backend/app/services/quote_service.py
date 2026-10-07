"""Quote upsert with confidence gate (TECH DOC §8.3, §9.2)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Quote, QuoteSource, Request, RequestStatus
from app.services.markup_service import apply_markup, load_rules


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

    if (
        source not in (QuoteSource.manual, QuoteSource.operator_corrected)
        and effective_confidence < settings.confidence_threshold
    ):
        return None

    if source in (QuoteSource.manual, QuoteSource.operator_corrected) and confidence is None:
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

    if price is not None and not bargain:
        await _apply_markup_from_request(
            session,
            quote,
            request_id,
            supplier_price=Decimal(str(price)),
        )
    elif price is not None and bargain:
        await _apply_markup_from_request(
            session,
            quote,
            request_id,
            use_bargain=True,
            supplier_price=Decimal(str(price)),
        )

    await _maybe_transition_to_priced(session, request_id)
    if quote is not None:
        request = await session.get(Request, request_id)
        if request is not None:
            try:
                from app.services.daily_sku_price_service import on_quote_upserted

                await on_quote_upserted(session, quote=quote, request=request)
            except Exception:
                from loguru import logger

                logger.exception(
                    "daily/best-quote projection failed request_id={} quote_id={}",
                    request_id,
                    quote.id,
                )
    return quote


async def _apply_markup_from_request(
    session: AsyncSession,
    quote: Quote,
    request_id: int,
    *,
    use_bargain: bool = False,
    supplier_price: Decimal | None = None,
) -> None:
    """Set markup_rub / price_final from supplier price and request product attrs."""
    request = await session.get(Request, request_id)
    if request is None:
        return
    if supplier_price is None:
        supplier_price = quote.price_bargain if use_bargain else quote.price_initial
    if supplier_price is None:
        return

    base = Decimal(supplier_price)
    if use_bargain and quote.markup_rub is not None:
        quote.price_final = base + quote.markup_rub
        await session.flush()
        return

    rules = await load_rules(session)
    product = request.normalized_json or {"model": request.source_text}
    final, markup, rule_id, _rule_key = apply_markup(base, product, rules)
    quote.markup_rub = markup
    quote.price_final = final
    quote.markup_rule_id = rule_id
    await session.flush()


def display_price_for_group(quote: Quote | None) -> Decimal | None:
    """Price shown to client group (marked-up final)."""
    if quote is None:
        return None
    if quote.price_final is not None:
        return quote.price_final
    return quote.price_bargain or quote.price_initial


async def _maybe_transition_to_priced(session: AsyncSession, request_id: int) -> None:
    """Set request status to priced on first quote, from open/awaiting_answers only."""
    result = await session.execute(select(Request).where(Request.id == request_id))
    request = result.scalar_one_or_none()
    if request is None:
        return

    if request.status not in (RequestStatus.open, RequestStatus.awaiting_answers):
        return

    has_priced_quote = await session.scalar(
        select(
            exists().where(
                Quote.request_id == request_id,
                Quote.price_initial.is_not(None),
            )
        )
    )
    if has_priced_quote:
        request.status = RequestStatus.priced
        await session.flush()
