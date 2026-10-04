"""Batch rebind of unbound supplier prices after RFQ."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageIn, Supplier
from app.parsers.regex_parser import parse_supplier_reply
from app.services.reply_binding_service import resolve_binding
from app.telegram.client import TelegramClientProtocol


async def retry_unbound_supplier_prices(
    session: AsyncSession,
    *,
    supplier: Supplier,
    telegram: TelegramClientProtocol,
) -> int:
    """Re-run binding for recent unbound price messages after a new RFQ."""
    from app.handlers.supplier_messages import (
        _notify_auto_bound,
        process_bound_supplier_reply,
    )

    window = get_settings().supplier_rebind_window_hours
    if window <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=window)
    result = await session.execute(
        select(MessageIn)
        .where(
            MessageIn.supplier_id == supplier.id,
            MessageIn.bind_status == "unbound",
            MessageIn.received_at >= cutoff,
        )
        .order_by(MessageIn.received_at.asc(), MessageIn.id.asc())
    )
    rebound = 0
    for message_in in result.scalars().all():
        parsed = parse_supplier_reply(message_in.raw_text)
        if parsed.price is None:
            continue
        decision = await resolve_binding(
            session,
            supplier=supplier,
            message={},
            raw_text=message_in.raw_text,
            reply_request=None,
        )
        if decision.status != "bound" or decision.request is None:
            continue
        quote = await process_bound_supplier_reply(
            session,
            request=decision.request,
            supplier=supplier,
            raw_text=message_in.raw_text,
            message_in=message_in,
            telegram=telegram,
            business_connection_id=message_in.business_connection_id,
            chat_id=message_in.chat_id,
            bind_method=f"rebind_{decision.method}",
            binding_price=decision.binding_price,
        )
        rebound += 1
        logger.info(
            "supplier_rebind message_in_id={} request_id={} method=rebind_{}",
            message_in.id,
            decision.request.id,
            decision.method,
        )
        if (
            decision.method == "order"
            and len(decision.candidates) > 1
            and quote is not None
        ):
            await _notify_auto_bound(
                session,
                message_in=message_in,
                supplier=supplier,
                request=decision.request,
                quote=quote,
                raw_text=message_in.raw_text,
                candidates=decision.candidates,
                telegram=telegram,
            )
    return rebound
