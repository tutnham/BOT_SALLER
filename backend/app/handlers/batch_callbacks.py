"""Telegram callbacks for request batch preview and lifecycle."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import RequestBatch
from app.services.batch_request_service import (
    BatchNotEditableError,
    BatchVersionConflictError,
    cancel_batch,
    compute_preview_stats,
    confirm_batch,
    load_batch_for_employee,
)
from app.services.client_publication_service import publish_batch_to_client
from app.services.purchase_confirmation_service import confirm_batch_purchases
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData, button, inline_keyboard
from app.templates.messages_ru import render_template
from app.utils.whitelist import get_employee_by_telegram_id

_PREVIEW_PAGE = 8


def _batch_button(text: str, action: str, arg: int = 0, page: int = 0) -> dict:
    return button(text, cd=CallbackData(namespace="batch", action=action, arg=arg, page=page))


async def send_batch_preview(
    session: AsyncSession,
    *,
    batch_id: int,
    chat_id: int,
    telegram: TelegramClientProtocol,
    page: int = 0,
) -> None:
    batch = await session.get(
        RequestBatch,
        batch_id,
        options=(selectinload(RequestBatch.requests),),
    )
    if batch is None:
        return
    stats = compute_preview_stats(batch.requests)
    ordered = sorted(batch.requests, key=lambda r: r.line_no or 0)
    start = page * _PREVIEW_PAGE
    chunk = ordered[start : start + _PREVIEW_PAGE]
    item_lines: list[str] = []
    for req in chunk:
        mark = ""
        if (req.normalization_confidence or 0) < 0.75 or not req.canonical_sku_key:
            mark = " ⚠"
        item_lines.append(
            render_template(
                "batch_preview_item",
                line_no=req.line_no,
                source_text=req.source_text,
                review_mark=mark,
            )
        )
    text = render_template(
        "batch_preview",
        batch_id=batch.id,
        recognized=stats.recognized,
        needs_review=stats.needs_review,
        duplicates=stats.duplicates,
        items_block="\n".join(item_lines) if item_lines else "—",
    )
    rows: list[list[dict[str, Any]]] = [
        [_batch_button("Подтвердить и отправить", "confirm", batch.id, batch.version)],
        [
            _batch_button("◀", "preview", batch.id, max(0, page - 1)),
            _batch_button("▶", "preview", batch.id, page + 1),
        ],
        [_batch_button("Отменить заявку", "cancel", batch.id, batch.version)],
    ]
    await telegram.send_message(chat_id, text, reply_markup=inline_keyboard(rows))


async def handle_batch_callback(
    session: AsyncSession,
    callback_query: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from_user = callback_query.get("from") or {}
    telegram_id = int(from_user.get("id") or 0)
    callback_id = callback_query.get("id", "")
    message = callback_query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    is_private = (message.get("chat") or {}).get("type") == "private"

    data = CallbackData.decode(callback_query.get("data"))
    if data is None or data.namespace != "batch":
        return "ignored"

    employee = await get_employee_by_telegram_id(session, telegram_id, require_active=True)
    if employee is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    batch = await load_batch_for_employee(
        session,
        batch_id=data.arg,
        employee_id=employee.id,
        group_chat_id=chat_id,
        is_private_chat=is_private,
    )
    if batch is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    await telegram.answer_callback_query(callback_id)

    if data.action == "preview":
        await send_batch_preview(
            session, batch_id=batch.id, chat_id=chat_id, telegram=telegram, page=data.page
        )
        return "ok"

    expected_version = data.page if data.action in {"confirm", "cancel"} else batch.version

    try:
        if data.action == "confirm":
            await confirm_batch(
                session,
                batch=batch,
                expected_version=expected_version,
                telegram=telegram,
            )
            await telegram.send_message(
                chat_id,
                render_template("batch_status_card", batch_id=batch.id, items_total=batch.items_total,
                                known_today=batch.items_priced, awaiting=batch.items_total,
                                unresolved=batch.items_unresolved, ready=0, no_price=0),
            )
        elif data.action == "cancel":
            await cancel_batch(session, batch=batch, expected_version=expected_version)
            await telegram.send_message(chat_id, f"Заявка-пакет #{batch.id} отменена.")
        elif data.action == "publish":
            await publish_batch_to_client(session, batch=batch, telegram=telegram)
            await telegram.send_message(chat_id, "Прайс отправлен клиенту.")
        elif data.action == "purchase":
            deals = await confirm_batch_purchases(
                session, batch=batch, employee_id=employee.id
            )
            await telegram.send_message(
                chat_id, f"Зафиксировано закупок: {len(deals)}"
            )
    except BatchVersionConflictError:
        await telegram.send_message(chat_id, "Данные устарели. Обновите карточку.")
    except BatchNotEditableError:
        await telegram.send_message(chat_id, "Пакет нельзя изменить в текущем статусе.")
    return "ok"
