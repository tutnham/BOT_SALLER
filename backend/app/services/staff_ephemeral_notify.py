"""Ephemeral price notifications visible only to staff in a client group chat."""

from __future__ import annotations

from decimal import Decimal

from loguru import logger
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Employee,
    Owner,
    PriceSelectionStatus,
    Quote,
    Request,
    RequestPriceSelection,
    RequestPriceState,
    Supplier,
)
from app.services.quote_service import display_price_for_group
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.templates.messages_ru import render_template


async def list_staff_ephemeral_recipient_ids(session: AsyncSession) -> list[int]:
    """Active employees and all owners (deduped, stable order)."""
    employee_ids = [
        int(row)
        for row in (
            await session.execute(
                select(Employee.telegram_id).where(Employee.active.is_(True))
            )
        ).scalars()
    ]
    owner_ids = [
        int(row)
        for row in (await session.execute(select(Owner.telegram_id))).scalars()
    ]
    seen: set[int] = set()
    unique: list[int] = []
    for chat_id in (*employee_ids, *owner_ids):
        if chat_id not in seen:
            seen.add(chat_id)
            unique.append(chat_id)
    return unique


async def other_suppliers_have_priced_quote(
    session: AsyncSession,
    request_id: int,
    exclude_supplier_id: int,
) -> bool:
    """True when another supplier already has a purchase price on this request."""
    return bool(
        await session.scalar(
            select(
                exists().where(
                    Quote.request_id == request_id,
                    Quote.supplier_id != exclude_supplier_id,
                    or_(
                        Quote.price_initial.is_not(None),
                        Quote.price_bargain.is_not(None),
                    ),
                )
            )
        )
    )


async def _client_price_already_public(session: AsyncSession, request: Request) -> bool:
    """Канальная цена уже ушла клиенту: ответ поставщика остаётся только у сотрудников."""
    if request.price_state == RequestPriceState.published:
        return True
    published = await session.scalar(
        select(RequestPriceSelection.id).where(
            RequestPriceSelection.request_id == request.id,
            RequestPriceSelection.status == PriceSelectionStatus.published.value,
        )
    )
    return published is not None


def should_use_public_first_quote_card(
    existing_quote: Quote | None,
    other_suppliers_priced: bool,
) -> bool:
    """First client-facing card in the group — no supplier identity."""
    return existing_quote is None and not other_suppliers_priced


def purchase_price_for_staff(quote: Quote | None) -> Decimal | None:
    if quote is None:
        return None
    return quote.price_bargain or quote.price_initial


async def notify_staff_ephemeral_in_group(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    group_chat_id: int,
    text: str,
    request_id: int,
    reply_markup: dict[str, object] | None = None,
) -> int:
    """Fan-out ephemeral copies to each staff member. Returns successful sends."""
    recipients = await list_staff_ephemeral_recipient_ids(session)
    if not recipients:
        logger.warning(
            "no staff recipients for ephemeral notify request_id={}", request_id
        )
        return 0

    delivered = 0
    for receiver_user_id in recipients:
        try:
            await telegram.send_message(
                group_chat_id,
                text,
                reply_markup=reply_markup,
                ephemeral_message_parameters={
                    "receiver_user_id": receiver_user_id,
                },
            )
        except TelegramSendError as exc:
            logger.warning(
                "ephemeral notify failed request_id={} group_chat_id={} "
                "receiver_user_id={}: {}",
                request_id,
                group_chat_id,
                receiver_user_id,
                exc,
            )
            continue
        delivered += 1
    return delivered


async def deliver_request_group_price_notice(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    request: Request,
    *,
    supplier: Supplier,
    existing_quote_before: Quote | None,
    public_text: str | None = None,
    staff_text: str,
    force_staff_ephemeral: bool = False,
) -> None:
    """Public first client card, or ephemeral staff-only for later supplier prices."""
    if force_staff_ephemeral or await _client_price_already_public(session, request):
        await notify_staff_ephemeral_in_group(
            session,
            telegram,
            group_chat_id=int(request.group_chat_id),
            text=staff_text,
            request_id=request.id,
        )
        return

    other_priced = await other_suppliers_have_priced_quote(
        session, request.id, supplier.id
    )
    if should_use_public_first_quote_card(existing_quote_before, other_priced):
        text = public_text or staff_text
        try:
            await telegram.send_message(int(request.group_chat_id), text)
        except TelegramSendError as exc:
            logger.warning(
                "group price notify failed request_id={} chat_id={}: {}",
                request.id,
                request.group_chat_id,
                exc,
            )
        return

    await notify_staff_ephemeral_in_group(
        session,
        telegram,
        group_chat_id=int(request.group_chat_id),
        text=staff_text,
        request_id=request.id,
    )


def render_staff_quote_parsed(
    *,
    request_id: int,
    supplier: Supplier,
    quote: Quote,
    available: bool | None = None,
    qty: int | None = None,
) -> str:
    return render_template(
        "supplier_quote_staff",
        request_id=request_id,
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        purchase_price=purchase_price_for_staff(quote),
        price=display_price_for_group(quote),
        available=available,
        qty=qty,
    )


def render_staff_low_confidence(
    *,
    request_id: int,
    supplier: Supplier,
    raw_text: str,
) -> str:
    return render_template(
        "supplier_low_confidence_staff",
        request_id=request_id,
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        raw_text=raw_text,
    )


def render_staff_price_changed(
    *,
    request_id: int,
    supplier: Supplier,
    old_price: Decimal,
    new_price: Decimal,
) -> str:
    return render_template(
        "price_changed_staff",
        request_id=request_id,
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        old_price=old_price,
        new_price=new_price,
    )