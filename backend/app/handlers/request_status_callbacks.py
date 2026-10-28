"""Callbacks for the request status card."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Request, RequestStatus
from app.services.request_card_service import (
    render_all_prices,
    render_request_card,
    request_card_keyboard,
)
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData
from app.utils.whitelist import get_employee_by_telegram_id, get_owner_by_telegram_id


async def handle_request_status_callback(
    session: AsyncSession,
    callback_query: dict[str, Any],
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from_user = callback_query.get("from") or {}
    from_id_raw = from_user.get("id")
    if from_id_raw is None:
        return "ignored"
    from_id = int(from_id_raw)
    callback_id = callback_query.get("id", "")

    owner = await get_owner_by_telegram_id(session, from_id)
    employee = await get_employee_by_telegram_id(session, from_id)
    if owner is None and employee is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    cd = CallbackData.decode(callback_query.get("data"))
    if cd is None or cd.namespace != "reqstatus":
        return "ignored"
    request = await session.get(Request, cd.arg)
    if request is None:
        await telegram.answer_callback_query(callback_id, text="Заявка не найдена")
        return "ignored"

    message = callback_query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id") or from_id)

    if cd.action == "prices":
        await telegram.answer_callback_query(callback_id)
        await telegram.send_message(
            chat_id, await render_all_prices(session, request.id, page=cd.page)
        )
        return "ok"

    if cd.action == "refresh":
        await telegram.answer_callback_query(callback_id)
        await telegram.send_message(
            chat_id,
            await render_request_card(session, request),
            reply_markup=request_card_keyboard(request),
        )
        return "ok"

    if cd.action == "close":
        if request.status in (RequestStatus.closed, RequestStatus.cancelled):
            await telegram.answer_callback_query(callback_id, text="Уже закрыта")
            return "ok"
        request.status = RequestStatus.closed
        await session.flush()
        await telegram.answer_callback_query(callback_id, text="Заявка закрыта")
        return "ok"

    await telegram.answer_callback_query(
        callback_id, text="Используйте команду в чате"
    )
    return "ok"
