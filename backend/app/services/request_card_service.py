"""Employee-facing request status card and the paginated "all prices" view."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    PriceSelectionStatus,
    Quote,
    Request,
    RequestPriceSelection,
    RequestStatus,
)
from app.services.best_quote_service import select_best_quote_for_request
from app.services.request_status_service import load_request_rfq_counters
from app.telegram.keyboards import CallbackData, button, inline_keyboard

PAGE_SIZE = 5


async def render_request_card(session: AsyncSession, request: Request) -> str:
    counters = await load_request_rfq_counters(session, request.id)
    published = await _published(session, request.id)
    best = await select_best_quote_for_request(session, request.id)

    lines = [
        f"Отправлено: {counters.sent_count} · доставлено: {counters.delivered_count}",
        f"Ответили: {counters.replied_count} · с ценой: {counters.quoted_count} · "
        f"на проверке: {counters.pending_review_count}",
    ]
    if published is not None and published.client_unit_price is not None:
        when = published.published_at.strftime("%H:%M") if published.published_at else "—"
        lines.append(f"Клиенту отправлено: {published.client_unit_price} ₽ в {when}")
    else:
        lines.append("Клиенту ещё не отправлено")
    if best.purchase_unit_price is not None:
        lines.append(
            f"Лучшая закупочная: {best.purchase_unit_price} ₽ · "
            f"для клиента: {best.client_unit_price} ₽"
        )
    return "\n".join(lines)


def request_card_keyboard(request: Request) -> dict:
    rows: list[list[dict]] = [
        [
            button("Все цены", cd=CallbackData(namespace="reqstatus", action="prices", arg=request.id)),
            button("Обновить", cd=CallbackData(namespace="reqstatus", action="refresh", arg=request.id)),
        ]
    ]
    if request.status not in (RequestStatus.closed, RequestStatus.cancelled):
        rows.append(
            [
                button("Отправить новую цену",
                       cd=CallbackData(namespace="reprice", action="ask", arg=request.id)),
                button("Торг", cd=CallbackData(namespace="reqstatus", action="bargain", arg=request.id)),
            ]
        )
        rows.append(
            [
                button("Перепроверить",
                       cd=CallbackData(namespace="reqstatus", action="recheck", arg=request.id)),
                button("Зафиксировать сделку",
                       cd=CallbackData(namespace="reqstatus", action="deal", arg=request.id)),
            ]
        )
    rows.append(
        [button("Закрыть", cd=CallbackData(namespace="reqstatus", action="close", arg=request.id))]
    )
    return inline_keyboard(rows)


async def render_all_prices(session: AsyncSession, request_id: int, *, page: int) -> str:
    result = await session.execute(
        select(Quote)
        .where(Quote.request_id == request_id)
        .options(selectinload(Quote.supplier))
        .order_by(Quote.price_initial.asc(), Quote.observed_at.asc(), Quote.id.asc())
    )
    quotes = list(result.scalars().all())
    published = await _published(session, request_id)
    best = await select_best_quote_for_request(session, request_id)

    valid = [q for q in quotes if q.confidence is None or q.confidence >= 0.75]
    review = [q for q in quotes if q not in valid]

    start = page * PAGE_SIZE
    chunk = valid[start : start + PAGE_SIZE]
    lines = [f"Цены по заявке #{request_id} (стр. {page + 1})"]
    if not chunk:
        lines.append("Нет цен")
    for quote in chunk:
        marks = []
        if published is not None and quote.id == published.selected_quote_id:
            marks.append("отправлена клиенту")
        if quote.id == best.selected_quote_id:
            marks.append("текущая лучшая")
        suffix = f" ({', '.join(marks)})" if marks else ""
        supplier_name = quote.supplier.name if quote.supplier is not None else "—"
        lines.append(f"{_price(quote)} ₽ — {supplier_name}{suffix}")
    if review:
        lines.append("Требуют проверки:")
        for quote in review:
            lines.append(f"{_price(quote)} ₽ — низкая уверенность")
    return "\n".join(lines)


def _price(quote: Quote) -> Decimal:
    return quote.price_bargain or quote.price_initial or Decimal("0")


async def _published(session: AsyncSession, request_id: int) -> RequestPriceSelection | None:
    result = await session.scalar(
        select(RequestPriceSelection).where(
            RequestPriceSelection.request_id == request_id,
            RequestPriceSelection.status == PriceSelectionStatus.published.value,
        )
    )
    return result
