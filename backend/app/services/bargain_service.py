"""Bargain workflow — template-only negotiation (TECH DOC §9.3)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageKind, MessageOut, Request, RequestStatus, Supplier
from app.db.unit_of_work import commit_or_flush
from app.services.deal_service import RequestNotFoundError, SupplierNotFoundError
from app.services.quote_service import select_best_quote
from app.services.routing_service import resolve_target
from app.services.supplier_delivery import deliver, is_sent
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template

BARGAIN_ALLOWED_STATUSES = (
    RequestStatus.awaiting_answers,
    RequestStatus.priced,
    RequestStatus.bargaining,
    RequestStatus.needs_recheck,
)


class BargainStatusError(Exception):
    """Request status does not allow bargaining."""


class NoEligibleQuoteError(Exception):
    """No quote with a usable price for this request."""


class SupplierUnavailableError(Exception):
    """Supplier cannot receive Bot API DM."""


async def start_bargain(
    session: AsyncSession,
    *,
    request_id: int,
    target_price: Decimal,
    telegram: TelegramClientProtocol,
    supplier_id: int | None = None,
) -> MessageOut:
    """
    Send template bargain message to resolved supplier.

    Raises:
        RequestNotFoundError, BargainStatusError, NoEligibleQuoteError,
        SupplierNotFoundError, SupplierUnavailableError
    """
    request = await session.get(Request, request_id)
    if request is None:
        raise RequestNotFoundError(request_id)

    if request.status not in BARGAIN_ALLOWED_STATUSES:
        raise BargainStatusError(request_id)

    if supplier_id is not None:
        supplier = await session.get(Supplier, supplier_id)
        if supplier is None:
            raise SupplierNotFoundError(supplier_id)
    else:
        best = await select_best_quote(session, request_id, prefer_bargain=False)
        if best is None or best.supplier is None:
            raise NoEligibleQuoteError(request_id)
        supplier = best.supplier

    if not supplier.active:
        raise SupplierUnavailableError(supplier.id)

    target = await resolve_target(session, supplier.id)
    if target is None:
        raise SupplierUnavailableError(supplier.id)

    # Commit status change before the external side-effect so a Telegram
    # failure cannot roll back an already delivered message.
    request.status = RequestStatus.bargaining
    await session.flush()
    await commit_or_flush(session)

    # Template must never include competitor prices (TECH DOC §9.3 / ТЗ §5).
    text = render_template(
        "bargain",
        request_id=request.id,
        target_price=target_price,
    )
    outbound = await deliver(
        session,
        telegram,
        supplier_id=supplier.id,
        chat_id=target.chat_id,
        text=text,
        kind=MessageKind.bargain,
        request_id=request.id,
        business_connection_id=target.business_connection_id,
    )
    if not is_sent(outbound):
        await commit_or_flush(session)
        raise SupplierUnavailableError(supplier.id)
    return outbound
