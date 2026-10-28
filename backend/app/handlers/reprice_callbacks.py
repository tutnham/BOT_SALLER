"""Employee callbacks for repricing a published client price."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reprice_service import confirm_reprice
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData
from app.utils.whitelist import get_employee_by_telegram_id, get_owner_by_telegram_id


async def handle_reprice_callback(
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
    if cd is None or cd.namespace != "reprice":
        return "ignored"

    if cd.action == "ignore":
        await telegram.answer_callback_query(callback_id, text="Оставлено без изменений")
        return "ok"

    if cd.action == "ask":
        await telegram.answer_callback_query(callback_id)
        message = callback_query.get("message") or {}
        await telegram.send_message(
            int(message.get("chat", {}).get("id") or from_id),
            "Подтвердить отправку новой цены клиенту?",
            reply_markup={
                "inline_keyboard": [[
                    {
                        "text": "Подтвердить",
                        "callback_data": CallbackData(
                            namespace="reprice", action="confirm", arg=cd.arg
                        ).encode(),
                    }
                ]]
            },
        )
        return "ok"

    if cd.action == "confirm":
        applied = await confirm_reprice(
            session,
            request_id=cd.arg,
            actor_employee_id=None if employee is None else employee.id,
            actor_telegram_id=from_id,
        )
        await telegram.answer_callback_query(
            callback_id,
            text="Новая цена отправлена клиенту" if applied else "Цена уже актуальна",
        )
        return "ok"

    return "ignored"
