"""Owner manual bind use case."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.binding import RequestNotBindableError, assert_request_bindable
from app.db.models import MessageIn, Request, Supplier
from app.telegram.client import TelegramClientProtocol


async def manual_bind_message(
    session: AsyncSession,
    *,
    message_in_id: int,
    request_id: int,
    telegram: TelegramClientProtocol,
) -> str:
    message_in = await session.get(MessageIn, message_in_id)
    if message_in is None:
        return "not_found"
    request = await session.get(Request, request_id)
    if request is None:
        return "request_not_found"
    supplier = await session.get(Supplier, message_in.supplier_id)
    if supplier is None:
        return "supplier_not_found"

    try:
        assert_request_bindable(request)
    except RequestNotBindableError:
        return "request_not_open"

    from app.handlers.supplier_messages import (
        _withdraw_quote,
        process_bound_supplier_reply,
    )

    if message_in.request_id == request_id and message_in.bind_status == "bound":
        return "already_bound"

    if message_in.request_id is not None and message_in.request_id != request_id:
        await _withdraw_quote(
            session, message_in=message_in, supplier=supplier, telegram=telegram
        )

    await process_bound_supplier_reply(
        session,
        request=request,
        supplier=supplier,
        raw_text=message_in.raw_text,
        message_in=message_in,
        telegram=telegram,
        business_connection_id=message_in.business_connection_id,
        chat_id=message_in.chat_id,
        bind_method="manual",
    )
    return "ok"
