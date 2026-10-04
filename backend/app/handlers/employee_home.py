"""Button home for employees: new request and own open requests."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import ClientGroup, Quote, Request, RequestStatus
from app.services import admin_service
from app.services.bargain_service import start_bargain
from app.services.deal_service import create_deal
from app.services.recheck_service import RECHECK_HOURS_DEFAULT, schedule_recheck
from app.services.request_service import create_request, load_request_for_employee
from app.telegram.client import TelegramClientProtocol
from app.telegram.home_buttons import EMP_MY_REQUESTS, EMP_NEW_REQUEST, EMPLOYEE_BUTTONS
from app.telegram.keyboards import CallbackData, button, inline_keyboard
from app.templates.messages_ru import format_supplier_label, render_template

_AWAIT_ASK = "emp_await_ask"
_AWAIT_PRICE = "emp_await_price"
_OPEN = (
    RequestStatus.open,
    RequestStatus.awaiting_answers,
    RequestStatus.priced,
    RequestStatus.bargaining,
    RequestStatus.needs_recheck,
)


def _emp_button(text: str, action: str, arg: int = 0) -> dict:
    return button(text, cd=CallbackData(namespace="emp", action=action, arg=arg))


async def employee_waiting_text(session: AsyncSession, telegram_id: int) -> bool:
    dialog = await admin_service.get_dialog(session, telegram_id)
    return dialog is not None and dialog.state in {_AWAIT_ASK, _AWAIT_PRICE}


async def handle_employee_home_text(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    telegram_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> bool:
    """Handle reply-keyboard labels and follow-up text. True if consumed."""
    text = (message.get("text") or message.get("caption") or "").strip()
    dialog = await admin_service.get_dialog(session, telegram_id)
    if dialog is not None and dialog.state == _AWAIT_ASK and text and text not in EMPLOYEE_BUTTONS:
        group_chat_id = int((dialog.payload or {}).get("group_chat_id") or 0)
        await admin_service.clear_dialog(session, telegram_id)
        if group_chat_id == 0:
            await telegram.send_message(chat_id, render_template("employee_no_client_group"))
            return True
        await create_request(
            session,
            group_chat_id=group_chat_id,
            employee_id=employee_id,
            source_text=text,
            telegram=telegram,
        )
        return True

    if dialog is not None and dialog.state == _AWAIT_PRICE and text and text not in EMPLOYEE_BUTTONS:
        await _apply_price_text(
            session,
            text=text,
            payload=dialog.payload or {},
            chat_id=chat_id,
            employee_id=employee_id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )
        await admin_service.clear_dialog(session, telegram_id)
        return True

    if text == EMP_NEW_REQUEST:
        await _begin_new_request(
            session,
            telegram_id=telegram_id,
            chat_id=chat_id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )
        return True

    if text == EMP_MY_REQUESTS:
        await _send_my_requests(
            session,
            employee_id=employee_id,
            chat_id=chat_id,
            telegram=telegram,
        )
        return True

    return False


async def handle_employee_callback(
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
    data = CallbackData.decode(callback_query.get("data"))
    if data is None or data.namespace != "emp":
        return "ignored"

    from app.utils.whitelist import get_employee_by_telegram_id

    employee = await get_employee_by_telegram_id(session, telegram_id, require_active=True)
    if employee is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"
    await telegram.answer_callback_query(callback_id)

    if data.action == "pick_group":
        group = await session.get(ClientGroup, data.arg)
        if group is None or not group.active:
            await telegram.send_message(chat_id, render_template("employee_no_client_group"))
            return "ok"
        await admin_service.set_dialog(
            session,
            telegram_id=telegram_id,
            state=_AWAIT_ASK,
            payload={"group_chat_id": group.chat_id},
        )
        await telegram.send_message(chat_id, render_template("employee_ask_prompt"))
        return "ok"

    if data.action == "req_open":
        await _send_request_actions(
            session,
            request_id=data.arg,
            employee_id=employee.id,
            chat_id=chat_id,
            telegram=telegram,
        )
        return "ok"

    if data.action == "req_cancel":
        from app.services.deal_service import cancel_request

        await cancel_request(session, request_id=data.arg)
        await telegram.send_message(
            chat_id, render_template("cancel_ok", request_id=data.arg)
        )
        return "ok"

    if data.action == "req_recheck":
        updated = await schedule_recheck(
            session, request_id=data.arg, hours=RECHECK_HOURS_DEFAULT
        )
        due_at = updated.recheck_at.isoformat() if updated.recheck_at else "—"
        await telegram.send_message(
            chat_id,
            render_template(
                "recheck_scheduled",
                request_id=data.arg,
                hours=RECHECK_HOURS_DEFAULT,
                due_at=due_at,
            ),
        )
        return "ok"

    if data.action in {"req_deal", "req_bargain"}:
        await admin_service.set_dialog(
            session,
            telegram_id=telegram_id,
            state=_AWAIT_PRICE,
            payload={"request_id": data.arg, "kind": data.action, "employee_id": employee.id},
        )
        await telegram.send_message(
            chat_id,
            render_template("employee_price_prompt", request_id=data.arg),
        )
        return "ok"

    return "ignored"


async def _begin_new_request(
    session: AsyncSession,
    *,
    telegram_id: int,
    chat_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> None:
    if not is_private_chat:
        await admin_service.set_dialog(
            session,
            telegram_id=telegram_id,
            state=_AWAIT_ASK,
            payload={"group_chat_id": chat_id},
        )
        await telegram.send_message(chat_id, render_template("employee_ask_prompt"))
        return

    groups = [
        group
        for group in await admin_service.list_client_groups(session)
        if group.active
    ]
    if not groups:
        await telegram.send_message(chat_id, render_template("employee_no_client_group"))
        return
    if len(groups) == 1:
        await admin_service.set_dialog(
            session,
            telegram_id=telegram_id,
            state=_AWAIT_ASK,
            payload={"group_chat_id": groups[0].chat_id},
        )
        await telegram.send_message(chat_id, render_template("employee_ask_prompt"))
        return
    rows = [
        [_emp_button(group.title or str(group.chat_id), "pick_group", group.id)]
        for group in groups[:8]
    ]
    await telegram.send_message(
        chat_id,
        render_template("employee_ask_pick_group"),
        reply_markup=inline_keyboard(rows),
    )


async def _send_my_requests(
    session: AsyncSession,
    *,
    employee_id: int,
    chat_id: int,
    telegram: TelegramClientProtocol,
) -> None:
    result = await session.execute(
        select(Request)
        .where(Request.employee_id == employee_id, Request.status.in_(_OPEN))
        .order_by(Request.id.desc())
        .limit(8)
    )
    requests = list(result.scalars().all())
    if not requests:
        await telegram.send_message(chat_id, render_template("employee_requests_empty"))
        return
    rows = [
        [
            _emp_button(
                f"#{item.id} {(item.source_text or '')[:32]}",
                "req_open",
                item.id,
            )
        ]
        for item in requests
    ]
    await telegram.send_message(
        chat_id,
        render_template("employee_requests_list"),
        reply_markup=inline_keyboard(rows),
    )


async def _send_request_actions(
    session: AsyncSession,
    *,
    request_id: int,
    employee_id: int,
    chat_id: int,
    telegram: TelegramClientProtocol,
) -> None:
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=True,
    )
    if request is None:
        await telegram.send_message(
            chat_id, render_template("request_not_found", request_id=request_id)
        )
        return
    text = render_template(
        "admin_request_detail",
        request_id=request.id,
        status_label=request.status.value,
        source_text=request.source_text,
    )
    rows = [
        [_emp_button("Перепроверка", "req_recheck", request.id)],
        [_emp_button("Торг", "req_bargain", request.id)],
        [_emp_button("Сделка", "req_deal", request.id)],
        [_emp_button("Отменить заявку", "req_cancel", request.id)],
    ]
    await telegram.send_message(chat_id, text, reply_markup=inline_keyboard(rows))


def _parse_supplier_and_price(text: str) -> tuple[int | None, Decimal | None]:
    parts = text.replace(",", ".").split()
    if len(parts) == 1:
        try:
            return None, Decimal(parts[0])
        except Exception:
            return None, None
    if len(parts) >= 2 and parts[0].isdigit():
        try:
            return int(parts[0]), Decimal(parts[1])
        except Exception:
            return None, None
    return None, None


async def _apply_price_text(
    session: AsyncSession,
    *,
    text: str,
    payload: dict,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> None:
    request_id = int(payload.get("request_id") or 0)
    kind = str(payload.get("kind") or "")
    supplier_id, price = _parse_supplier_and_price(text)
    if price is None:
        await telegram.send_message(chat_id, render_template("employee_price_prompt", request_id=request_id))
        return
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(chat_id, render_template("request_not_found", request_id=request_id))
        return
    if supplier_id is None:
        quotes = (
            await session.execute(
                select(Quote)
                .options(selectinload(Quote.supplier))
                .where(Quote.request_id == request_id)
            )
        ).scalars().all()
        if len(quotes) == 1:
            supplier_id = quotes[0].supplier_id
        else:
            await telegram.send_message(
                chat_id,
                render_template("employee_price_prompt", request_id=request_id),
            )
            return
    if kind == "req_deal":
        deal = await create_deal(
            session,
            request_id=request_id,
            supplier_id=supplier_id,
            final_price=price,
        )
        await telegram.send_message(
            chat_id,
            render_template(
                "deal_closed",
                request_id=request_id,
                supplier_label=format_supplier_label(
                    None, deal.chosen_supplier_id or supplier_id
                ),
                final_price=price,
            ),
        )
        return
    await start_bargain(
        session,
        request_id=request_id,
        target_price=price,
        telegram=telegram,
        supplier_id=supplier_id,
    )
    await telegram.send_message(
        chat_id,
        render_template(
            "bargain_sent",
            request_id=request_id,
            target_price=price,
            supplier_label=format_supplier_label(None, supplier_id),
        ),
    )
