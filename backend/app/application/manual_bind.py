"""Owner manual bind use case."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.binding import RequestNotBindableError, assert_request_bindable
from app.db.models import MessageIn, Request, Supplier, SupplierBindSession
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

    from app.services.supplier_bind_session_service import (
        cancel_bind_sessions_for_message,
        resolve_bind_session,
    )

    bind_row = await session.scalar(
        select(SupplierBindSession).where(
            SupplierBindSession.message_in_id == message_in_id,
            SupplierBindSession.status == "pending",
        )
    )
    if bind_row is not None:
        allowed = set(bind_row.candidate_request_ids or [])
        if request_id not in allowed:
            return "request_not_open"
        result = await resolve_bind_session(
            session,
            session_id=bind_row.id,
            request_id=request_id,
            resolution_method="manual",
            supplier=supplier,
            chat_id=message_in.chat_id,
            business_connection_id=message_in.business_connection_id,
            resolved_by_telegram_user_id=None,
            telegram=telegram,
        )
        if result in ("ok", "already_resolved"):
            return "ok"
        return "bind_failed"

    await cancel_bind_sessions_for_message(session, message_in_id=message_in.id)

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
