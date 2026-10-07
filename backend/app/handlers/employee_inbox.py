"""Employee operations inbox for unresolved supplier RFQ lines."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, Request, Supplier
from app.handlers.supplier_messages import manual_bind_message
from app.services.supplier_review_service import (
    employee_can_resolve_item,
    list_all_pending_items,
    list_pending_items_for_employee,
    lock_pending_item,
    resolve_item,
)
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData, button, inline_keyboard
from app.templates.messages_ru import render_template
from app.utils.whitelist import get_employee_by_telegram_id, get_owner_by_telegram_id


def _inbox_cb(action: str, arg: int = 0, page: int = 0) -> dict:
    return button(
        text=".",
        cd=CallbackData(namespace="inbox", action=action, arg=arg, page=page),
    )


async def send_pending_inbox(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
    employee: Employee | None,
    is_owner: bool,
) -> None:
    if is_owner:
        items = await list_all_pending_items(session)
    elif employee is not None:
        items = await list_pending_items_for_employee(session, employee=employee)
    else:
        return

    if not items:
        await telegram.send_message(chat_id, "Неразобранных ответов нет.")
        return

    for item in items[:10]:
        message_in = item.message_in
        supplier = message_in.supplier if message_in else None
        supplier_name = supplier.name if supplier else "—"
        attrs = (item.parsed_json or {}).get("attrs") or {}
        attrs_line = ", ".join(
            filter(
                None,
                [
                    attrs.get("model"),
                    attrs.get("storage"),
                    attrs.get("color"),
                    attrs.get("sim"),
                ],
            )
        )
        candidates = (item.parsed_json or {}).get("candidate_request_ids") or []
        candidates_line = ", ".join(f"#{cid}" for cid in candidates[:8]) or "—"
        age_minutes = int(
            (datetime.now(timezone.utc) - item.created_at).total_seconds() // 60
        )
        text = render_template(
            "supplier_review_card",
            supplier_name=supplier_name,
            raw_line=item.raw_text[:400],
            price_line=str(item.parsed_price) if item.parsed_price is not None else "—",
            attrs_line=attrs_line or "—",
            conflict_line=item.conflict_reason or "—",
            candidates_line=candidates_line,
            age_minutes=age_minutes,
        )
        rows: list[list[dict[str, Any]]] = []
        for request_id in candidates[:6]:
            rows.append(
                [
                    button(
                        f"К заявке #{request_id}",
                        cd=CallbackData(
                            namespace="inbox", action="bind", arg=item.id, page=request_id
                        ),
                    )
                ]
            )
        rows.append(
            [
                button(
                    "Игнорировать",
                    cd=CallbackData(namespace="inbox", action="ignore", arg=item.id),
                )
            ]
        )
        await telegram.send_message(chat_id, text, reply_markup=inline_keyboard(rows))


async def handle_inbox_callback(
    session: AsyncSession,
    callback_query: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from_user = callback_query.get("from") or {}
    telegram_id = int(from_user.get("id") or 0)
    data = CallbackData.decode(callback_query.get("data"))
    if data is None or data.namespace != "inbox":
        return "ignored"

    owner = await get_owner_by_telegram_id(session, telegram_id)
    employee = await get_employee_by_telegram_id(session, telegram_id)
    if owner is None and employee is None:
        return "ignored"

    message = callback_query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id") or 0)

    if data.action == "ignore":
        item = await lock_pending_item(session, data.arg)
        if item is None:
            return "ok"
        await resolve_item(session, item, request_id=None, bind_method="ignored")
        await telegram.send_message(chat_id, render_template("bind_ignored_ok"))
        return "ok"

    if data.action == "bind":
        item = await lock_pending_item(session, data.arg)
        if item is None:
            return "ok"
        request_id = data.page
        if employee is not None and owner is None:
            allowed = await employee_can_resolve_item(
                session, employee=employee, item=item, request_id=request_id
            )
            if not allowed:
                await telegram.send_message(chat_id, render_template("bind_failed", reason="нет доступа"))
                return "ok"

        message_in = item.message_in
        supplier = await session.get(Supplier, message_in.supplier_id)
        request = await session.get(Request, request_id)
        if supplier is None or request is None or message_in is None:
            return "ok"

        await manual_bind_message(
            session,
            message_in_id=message_in.id,
            request_id=request_id,
            telegram=telegram,
        )
        await resolve_item(session, item, request_id=request_id, bind_method="employee")
        await telegram.send_message(
            chat_id,
            render_template("bind_ok", request_id=request_id),
        )
        return "ok"

    return "ignored"
