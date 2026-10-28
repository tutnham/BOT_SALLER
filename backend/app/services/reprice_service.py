"""Repricing a published client price and better-late-price notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    PriceSelectionStatus,
    Request,
    RequestPriceSelection,
    RequestPriceState,
)
from app.services.audit_service import record_admin_mutation
from app.services.best_quote_service import select_best_quote_for_request
from app.services.first_price_publication_service import (
    _enqueue_client_message,
    _next_selection_version,
)
from app.templates.messages_ru import render_template

REPRICE_KEY = "request:{request_id}:reprice:v{version}"


def reprice_dedupe_key(request_id: int, version: int) -> str:
    return REPRICE_KEY.format(request_id=request_id, version=version)


async def notify_better_late_price(
    session: AsyncSession,
    *,
    request_id: int,
) -> bool:
    """Tell employees when a later same-day quote beats the published price."""
    settings = get_settings()
    if not settings.notify_employee_on_better_late_price:
        return False
    published = await _published_selection(session, request_id)
    if published is None or published.purchase_unit_price is None:
        return False
    result = await select_best_quote_for_request(session, request_id)
    if (
        result.purchase_unit_price is None
        or result.selected_quote_id == published.selected_quote_id
        or result.purchase_unit_price >= published.purchase_unit_price
    ):
        return False

    from app.services.telemetry import record_better_late_quote

    record_better_late_quote()
    from app.telegram.keyboards import CallbackData, button, inline_keyboard

    buttons = inline_keyboard(
        [[
            button("Отправить новую цену клиенту",
                   cd=CallbackData(namespace="reprice", action="ask", arg=request_id)),
            button("Игнорировать",
                   cd=CallbackData(namespace="reprice", action="ignore", arg=request_id)),
        ]]
    )
    from app.services.alert_service import list_operator_destinations

    text = render_template(
        "better_late_price_employee",
        request_id=request_id,
        client_price=result.client_unit_price,
    )
    key = f"request:{request_id}:better-late:q{result.selected_quote_id}"
    for chat_id in await list_operator_destinations(session):
        await _enqueue_client_message(
            session,
            dedupe_key=f"{key}:{chat_id}",
            chat_id=chat_id,
            text=text,
            request_id=request_id,
            reply_markup=buttons,
        )
    return True


async def confirm_reprice(
    session: AsyncSession,
    *,
    request_id: int,
    actor_employee_id: int | None,
    actor_telegram_id: int | None,
) -> bool:
    """Replace the published price with the current best, in one transaction."""
    published = await _published_selection(session, request_id)
    if published is None:
        return False
    result = await select_best_quote_for_request(session, request_id)
    if result.selected_quote_id is None or result.client_unit_price is None:
        return False
    if result.selected_quote_id == published.selected_quote_id:
        return False

    request = await session.get(Request, request_id)
    if request is None:
        return False

    version = await _next_selection_version(session, request_id)
    moment = datetime.now(UTC)
    published.status = PriceSelectionStatus.superseded.value
    await session.flush()

    new_row = RequestPriceSelection(
        request_id=request_id,
        batch_id=request.batch_id,
        canonical_sku_key=request.canonical_sku_key,
        status=PriceSelectionStatus.published.value,
        selected_quote_id=result.selected_quote_id,
        selected_supplier_id=result.selected_supplier_id,
        purchase_unit_price=result.purchase_unit_price,
        requested_qty=result.requested_qty,
        client_unit_price=result.client_unit_price,
        selection_reason="employee_reprice",
        candidate_count=result.candidate_count,
        selection_version=version,
        selected_at=moment,
        published_at=moment,
        published_by=actor_employee_id,
        publication_reason="employee_reprice",
    )
    session.add(new_row)
    await session.flush()
    published.superseded_by_id = new_row.id

    await _enqueue_client_message(
        session,
        dedupe_key=reprice_dedupe_key(request_id, version),
        chat_id=request.group_chat_id,
        text=render_template(
            "client_price_updated", request_id=request_id, price=result.client_unit_price
        ),
        request_id=request_id,
    )
    request.price_state = RequestPriceState.published
    await record_admin_mutation(
        session,
        action="price_reprice",
        entity_type="request",
        entity_id=str(request_id),
        actor_telegram_id=actor_telegram_id,
        previous_state={
            "selection_id": published.id,
            "client_unit_price": _dec(published.client_unit_price),
        },
        new_state={
            "selection_id": new_row.id,
            "client_unit_price": _dec(result.client_unit_price),
        },
    )
    await session.flush()
    from app.services.telemetry import record_manual_reprice

    record_manual_reprice()
    return True


async def _published_selection(
    session: AsyncSession, request_id: int
) -> RequestPriceSelection | None:
    result = await session.scalar(
        select(RequestPriceSelection).where(
            RequestPriceSelection.request_id == request_id,
            RequestPriceSelection.status == PriceSelectionStatus.published.value,
        )
    )
    return result


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
