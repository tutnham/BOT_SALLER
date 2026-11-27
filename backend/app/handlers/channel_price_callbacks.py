"""Подтверждение цены из прайс-канала по заявке (оффер из channel_sku_price_service)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Request
from app.services.channel_sku_price_service import (
    confirm_channel_price_offer,
    dismiss_channel_price_offer,
)
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData
from app.utils.whitelist import resolve_price_approver

_OUTCOME_TEXT = {
    "published": "Цена отправлена клиенту",
    "already": "Клиенту уже отправлена цена",
    "stale": "Цена прайса изменилась или истекла",
    "closed": "Заявка закрыта",
    "no_offer": "Оффер не найден",
}
_DISMISSED_TEXT = "Оффер отменён"


async def handle_channel_price_callback(
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

    cd = CallbackData.decode(callback_query.get("data"))
    if cd is None or cd.namespace != "chanprice" or cd.action not in ("confirm", "dismiss"):
        return "ignored"

    approver = await resolve_price_approver(session, from_id)
    if approver is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    request_id = cd.arg
    if approver.employee_id is not None:
        request = await session.get(Request, request_id)
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        callback_chat_id = chat.get("id")
        allowed = request is not None and (
            (chat.get("type") == "private" and request.employee_id == approver.employee_id)
            or (
                chat.get("type") != "private"
                and callback_chat_id is not None
                and int(request.group_chat_id) == int(callback_chat_id)
            )
        )
        if not allowed:
            await telegram.answer_callback_query(callback_id, text="Нет доступа")
            return "ignored"

    if cd.action == "dismiss":
        await dismiss_channel_price_offer(
            session, request_id=request_id, actor_telegram_id=from_id
        )
        await telegram.answer_callback_query(callback_id, text=_DISMISSED_TEXT)
        await _remove_buttons(callback_query, telegram, text=f"{_DISMISSED_TEXT} · #{request_id}")
        return "ok"

    outcome = await confirm_channel_price_offer(
        session,
        request_id=request_id,
        actor_employee_id=approver.employee_id,
        actor_telegram_id=from_id,
    )
    text = _OUTCOME_TEXT[outcome]
    await telegram.answer_callback_query(
        callback_id, text=text, show_alert=outcome != "published"
    )
    if outcome in ("published", "already", "stale"):
        await _remove_buttons(callback_query, telegram, text=f"{text} · #{request_id}")
    return "ok"


async def _remove_buttons(
    callback_query: dict[str, Any],
    telegram: TelegramClientProtocol,
    *,
    text: str,
) -> None:
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await telegram.edit_message_text(
            int(chat_id), int(message_id), text, reply_markup=None
        )
    except Exception:
        pass
