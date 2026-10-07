"""Owner/admin inline menu for managing suppliers, chats, groups, channels.

All DB mutations are delegated to admin_service.py; this module only handles
Telegram UI and callback data.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Employee,
    PendingChat,
    ProductCategory,
    RequestStatus,
    Supplier,
)
from app.services import admin_service, parser_client, purge_service
from app.services.deal_service import (
    RequestNotFoundError,
    RequestNotOpenError,
    cancel_request,
)
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.telegram.home_buttons import EMP_PENDING_REPLIES, OWNER_MENU
from app.telegram.keyboards import (
    CallbackData,
    button,
    inline_keyboard,
    menu_button,
    paginated_keyboard,
)
from app.templates.messages_ru import render_template
from app.utils.telegram import (
    extract_message_text,
    resolve_user_id_from_message,
)
from app.utils.whitelist import get_owner_by_telegram_id

_PAGE_SIZE = 8
_REQUEST_STATUS_LABELS = {
    RequestStatus.open: "новая",
    RequestStatus.awaiting_answers: "ждём ответы",
    RequestStatus.priced: "есть цены",
    RequestStatus.bargaining: "торг",
    RequestStatus.needs_recheck: "перепроверка",
    RequestStatus.closed: "закрыта",
    RequestStatus.cancelled: "отменена",
}
_MAX_REQUEST_SNIPPET = 48
_CATEGORY_ORDER: tuple[ProductCategory, ...] = (
    ProductCategory.apple,
    ProductCategory.samsung,
    ProductCategory.power_station,
    ProductCategory.other,
)
_CATEGORY_LABELS = {
    ProductCategory.apple: "Apple",
    ProductCategory.samsung: "Samsung",
    ProductCategory.power_station: "Электростанции",
    ProductCategory.other: "Прочее",
}


def _cb(action: str, arg: int = 0, page: int = 0) -> CallbackData:
    return CallbackData(namespace="admin", action=action, arg=arg, page=page)


async def _send_or_edit(
    telegram: TelegramClientProtocol,
    *,
    chat_id: int,
    text: str,
    message_id: int | None = None,
    markup: dict[str, Any] | None = None,
) -> None:
    if message_id is not None:
        try:
            await telegram.edit_message_text(
                chat_id, message_id, text, reply_markup=markup
            )
            return
        except Exception:
            pass
    await telegram.send_message(chat_id, text, reply_markup=markup)


async def handle_admin_message(
    session: AsyncSession,
    message: dict[str, Any],
    *,
    telegram: TelegramClientProtocol | None = None,
) -> str:
    telegram = telegram or get_telegram_client()
    from_user = message.get("from") or {}
    owner_id = int(from_user.get("id", 0))
    chat_id = int((message.get("chat") or {}).get("id", 0))
    text = extract_message_text(message)

    owner = await get_owner_by_telegram_id(session, owner_id)
    if owner is None:
        return "ignored"

    dialog = await admin_service.get_dialog(session, owner_id)
    if dialog is not None:
        return await _handle_dialog_text(
            session, dialog, owner_id, chat_id, message, telegram
        )

    if text == EMP_PENDING_REPLIES:
        from app.handlers.employee_inbox import send_pending_inbox

        await send_pending_inbox(
            session,
            chat_id=chat_id,
            telegram=telegram,
            employee=None,
            is_owner=True,
        )
        return "ok"

    if text == OWNER_MENU or text.startswith("/menu") or text.startswith("/admin") or text.startswith("/"):
        await _send_main_menu(session, telegram, chat_id)
        return "ok"

    await telegram.send_message(chat_id, render_template("admin_unknown_command"))
    return "ok"


async def handle_admin_callback(
    session: AsyncSession,
    callback_query: dict[str, Any],
) -> str:
    telegram = get_telegram_client()
    from_user = callback_query.get("from") or {}
    owner_id = int(from_user.get("id", 0))
    callback_id = callback_query.get("id", "")

    owner = await get_owner_by_telegram_id(session, owner_id)
    if owner is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    await telegram.answer_callback_query(callback_id)

    message = callback_query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id", 0))
    message_id = message.get("message_id")

    raw_data = callback_query.get("data")
    cd = CallbackData.decode(raw_data)
    if cd is None or cd.namespace != "admin":
        return "ignored"

    try:
        await _dispatch_callback(session, cd, owner_id, chat_id, message_id, telegram)
    except Exception as exc:
        logger.warning("Admin callback failed owner_id={} cd={}: {}", owner_id, cd, exc)
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_error", detail="операция не выполнена"),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
        )
        return "error"
    return "ok"


async def _dispatch_callback(
    session: AsyncSession,
    cd: CallbackData,
    owner_id: int,
    chat_id: int,
    message_id: int | None,
    telegram: TelegramClientProtocol,
) -> None:
    action = cd.action

    if action == "main_menu":
        await _send_main_menu(session, telegram, chat_id, message_id=message_id)
        return

    if action == "dlq":
        await _send_dlq_list(session, telegram, chat_id, message_id=message_id)
        return

    if action in ("dlqin", "dlqout"):
        await _send_dlq_item(
            session,
            telegram,
            chat_id,
            queue="inbox" if action == "dlqin" else "outbox",
            row_id=cd.arg,
            message_id=message_id,
        )
        return

    if action == "dlqin_go":
        from app.services.dlq_service import replay_inbox

        outcome = await replay_inbox(session, cd.arg, actor_telegram_id=owner_id)
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text="Повтор поставлен в очередь." if outcome == "replayed" else "Строка уже закрыта.",
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К ошибкам", "dlq")]]),
        )
        return

    if action == "dlqout_go":
        from app.services.dlq_service import ReplayNeedsConfirmation, replay_outbox

        try:
            outcome = await replay_outbox(
                session,
                cd.arg,
                actor_telegram_id=owner_id,
                confirmed=True,
            )
        except ReplayNeedsConfirmation:
            outcome = "confirm"
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text="Повтор поставлен в очередь." if outcome == "replayed" else "Строка уже закрыта.",
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К ошибкам", "dlq")]]),
        )
        return

    if action in ("dlqin_ok", "dlqout_ok"):
        from app.services.dlq_service import resolve_row

        outcome = await resolve_row(
            session,
            queue="inbox" if action == "dlqin_ok" else "outbox",
            row_id=cd.arg,
            actor_telegram_id=owner_id,
            reason="operator_resolved",
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text="Закрыто без повтора." if outcome == "resolved" else "Строка уже закрыта.",
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К ошибкам", "dlq")]]),
        )
        return

    if action == "report_day":
        from app.handlers.owner_commands import send_owner_report

        await send_owner_report(session, chat_id=chat_id, period="day", telegram=telegram)
        return

    if action == "report_week":
        from app.handlers.owner_commands import send_owner_report

        await send_owner_report(session, chat_id=chat_id, period="week", telegram=telegram)
        return

    if action == "report_purchases":
        from app.handlers.purchase_report_commands import handle_purchase_report_command

        await handle_purchase_report_command(
            session,
            text="/purchase_report today",
            chat_id=chat_id,
            telegram_id=owner_id,
            telegram=telegram,
        )
        return

    if action == "markup_list":
        from app.handlers.owner_commands import send_markup_list

        await send_markup_list(session, chat_id=chat_id, telegram=telegram)
        return

    if action == "mk_add":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_markup_rule_phrase",
            payload={},
        )
        await telegram.send_message(
            chat_id,
            render_template("markup_add_enter_phrase"),
        )
        return

    if action == "mk_open":
        from app.handlers.owner_commands import send_markup_edit

        await send_markup_edit(
            session, chat_id=chat_id, rule_id=cd.arg, telegram=telegram
        )
        return

    if action == "mk_set":
        from decimal import Decimal

        from app.handlers.owner_commands import apply_markup_choice

        await apply_markup_choice(
            session,
            chat_id=chat_id,
            rule_id=cd.arg,
            amount=Decimal(cd.page),
            telegram=telegram,
        )
        return

    if action == "mk_custom":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_markup_amount",
            payload={"rule_id": cd.arg},
        )
        from app.db.models import MarkupRule

        rule = await session.get(MarkupRule, cd.arg)
        label = (rule.rule_key if rule and rule.rule_key else "правило").replace("_", " ")
        await telegram.send_message(
            chat_id,
            render_template("markup_enter_amount", rule_label=label),
        )
        return

    if action == "bind_pick":
        from app.handlers.supplier_messages import manual_bind_message

        result = await manual_bind_message(
            session,
            message_in_id=cd.arg,
            request_id=cd.page,
            telegram=telegram,
        )
        if result == "ok":
            text = render_template("bind_ok", request_id=cd.page)
        elif result == "already_bound":
            text = render_template("bind_already", request_id=cd.page)
        else:
            text = render_template("bind_failed", reason=result)
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "bind_unbind":
        from app.handlers.supplier_messages import unbind_message

        result = await unbind_message(session, message_in_id=cd.arg, telegram=telegram)
        text = (
            render_template("bind_unbound_ok")
            if result == "ok"
            else render_template("bind_failed", reason=result)
        )
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "bind_ignore":
        from app.handlers.supplier_messages import unbind_message

        result = await unbind_message(
            session, message_in_id=cd.arg, telegram=telegram, status="ignored"
        )
        text = (
            render_template("bind_ignored_ok")
            if result == "ok"
            else render_template("bind_failed", reason=result)
        )
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "bind_stop":
        try:
            await cancel_request(session, request_id=cd.arg)
            text = render_template("bind_stop_ok", request_id=cd.arg)
        except RequestNotFoundError:
            text = render_template("purge_request_not_found", request_id=cd.arg)
        except RequestNotOpenError:
            text = render_template(
                "admin_request_already_done", request_id=cd.arg
            )
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "requests":
        await _send_request_list(session, telegram, chat_id, cd.page, message_id=message_id)
        return

    if action == "req_detail":
        await _send_request_detail(
            session, telegram, chat_id, cd.arg, list_page=cd.page, message_id=message_id
        )
        return

    if action == "req_cancel":
        try:
            await cancel_request(session, request_id=cd.arg)
        except RequestNotFoundError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("purge_request_not_found", request_id=cd.arg),
                message_id=message_id,
                markup=inline_keyboard([[menu_button("К заявкам", "requests")]]),
            )
            return
        except RequestNotOpenError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_request_already_done", request_id=cd.arg),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К заявке", "req_detail", cd.arg, cd.page)]]
                ),
            )
            return
        await _send_request_detail(
            session, telegram, chat_id, cd.arg, list_page=cd.page, message_id=message_id
        )
        return

    if action == "req_purge_ask":
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_request_purge_confirm", request_id=cd.arg),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("Удалить", "req_purge", cd.arg, cd.page)],
                    [menu_button("Отмена", "req_detail", cd.arg, cd.page)],
                ]
            ),
        )
        return

    if action == "req_purge":
        deleted = await purge_service.hard_delete_request(session, cd.arg)
        text = render_template(
            "purge_request_ok" if deleted else "purge_request_not_found",
            request_id=cd.arg,
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К заявкам", "requests", page=cd.page)]]),
        )
        return

    if action == "business_status":
        await _send_business_status(session, telegram, chat_id, message_id=message_id)
        return

    if action == "suppliers":
        await _send_supplier_list(session, telegram, chat_id, cd.page, message_id=message_id)
        return

    if action == "employees":
        await _send_employee_list(session, telegram, chat_id, cd.page, message_id=message_id)
        return

    if action == "employee_detail":
        await _send_employee_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "employee_add_start":
        await admin_service.set_dialog(
            session, telegram_id=owner_id, state="await_employee_name", payload={}
        )
        text = render_template("admin_await_employee_name")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "employees", page=0)]]),
        )
        return

    if action == "employee_toggle_active":
        await admin_service.toggle_employee_active(session, cd.arg)
        await _send_employee_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "emp_del_ask":
        employee = await session.get(Employee, cd.arg)
        if employee is None:
            await _send_employee_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template(
                "admin_employee_delete_confirm",
                employee_id=employee.id,
                employee_name=employee.name or "—",
            ),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("Удалить", "emp_del", employee.id)],
                    [menu_button("Отмена", "employee_detail", employee.id)],
                ]
            ),
        )
        return

    if action == "emp_del":
        try:
            employee = await admin_service.delete_employee(session, cd.arg)
        except admin_service.EmployeeNotFoundError:
            await _send_employee_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        except admin_service.EmployeeHasRequestsError as exc:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template(
                    "admin_employee_has_requests",
                    employee_id=exc.employee_id,
                    count=exc.request_count,
                ),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К сотруднику", "employee_detail", cd.arg)]]
                ),
            )
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_employee_deleted", employee_id=employee.id),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К сотрудникам", "employees")]]),
        )
        return

    if action == "supplier_detail":
        await _send_supplier_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "supplier_add_start":
        await admin_service.set_dialog(
            session, telegram_id=owner_id, state="await_supplier_name", payload={}
        )
        text = render_template("admin_await_supplier_name")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "main_menu")]]),
        )
        return

    if action == "supplier_rename_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_rename",
            payload={"supplier_id": cd.arg},
        )
        text = render_template("admin_await_rename")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "supplier_detail", cd.arg)]]),
        )
        return

    if action == "supplier_toggle_active":
        await admin_service.toggle_supplier_active(session, cd.arg)
        await _send_supplier_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "supplier_toggle_rfq":
        await admin_service.toggle_supplier_rfq(session, cd.arg)
        await _send_supplier_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "sup_cat":
        await _send_supplier_categories(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "sup_cat_tog":
        if 0 <= cd.page < len(_CATEGORY_ORDER):
            category = _CATEGORY_ORDER[cd.page]
            try:
                await admin_service.toggle_supplier_category(
                    session, cd.arg, category.value
                )
            except admin_service.SupplierNotFoundError:
                await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
                return
        await _send_supplier_categories(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "sup_del_ask":
        supplier = await session.get(Supplier, cd.arg)
        if supplier is None:
            await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template(
                "admin_supplier_delete_confirm",
                supplier_id=supplier.id,
                supplier_name=supplier.name or "—",
            ),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("Удалить", "sup_del", supplier.id)],
                    [menu_button("Отмена", "supplier_detail", supplier.id)],
                ]
            ),
        )
        return

    if action == "sup_del":
        try:
            supplier = await admin_service.delete_supplier(session, cd.arg)
        except admin_service.SupplierNotFoundError:
            await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_deleted", supplier_id=supplier.id),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К поставщикам", "suppliers")]]),
        )
        return

    if action == "supplier_chats":
        await _send_supplier_chats(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_channel":
        await _send_supplier_channel_list(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_bind_ch":
        supplier_id = cd.arg
        parser_pk = cd.page
        try:
            channels = await parser_client.list_channels()
        except parser_client.ParserClientError as exc:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_price_channels_error", detail=str(exc)),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К поставщику", "supplier_detail", supplier_id)]]
                ),
            )
            return
        channel = next((ch for ch in channels if ch.id == parser_pk), None)
        if channel is None or channel.channel_id is None or not channel.is_active:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_channel_not_ready"),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К каналам", "supplier_channel", supplier_id)]]
                ),
            )
            return
        try:
            await admin_service.bind_supplier_price_channel(
                session,
                supplier_id=supplier_id,
                channel_id=int(channel.channel_id),
                username=channel.username or channel.title,
            )
        except admin_service.PriceChannelBusyError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_channel_taken"),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К каналам", "supplier_channel", supplier_id)]]
                ),
            )
            return
        await _send_supplier_detail(
            session, telegram, chat_id, supplier_id, message_id=message_id
        )
        return

    if action == "supplier_unbind_ch":
        await admin_service.unbind_supplier_price_channel(session, cd.arg)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_tgid_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_telegram_id",
            payload={"supplier_id": cd.arg},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_tgid_prompt"),
            message_id=message_id,
            markup=inline_keyboard(
                [[menu_button("Отмена", "supplier_detail", cd.arg)]]
            ),
        )
        return

    if action == "supplier_tgid_clear":
        await admin_service.unbind_supplier_telegram_id(session, cd.arg)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_bind_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_bind",
            payload={"supplier_id": cd.arg},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_bind_prompt"),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [
                        menu_button("↩️ Переслать сообщение", "supplier_bind_forward", cd.arg),
                        menu_button("🔗 Ссылка", "supplier_bind_link", cd.arg),
                    ],
                    [menu_button("⏭ Пропустить", "supplier_bind_skip", cd.arg)],
                    [menu_button("Отмена", "supplier_detail", cd.arg)],
                ]
            ),
        )
        return

    if action == "supplier_bind_forward":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_forward",
            payload={"supplier_id": cd.arg},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_forward_prompt"),
            message_id=message_id,
            markup=inline_keyboard(
                [[menu_button("Отмена", "supplier_detail", cd.arg)]]
            ),
        )
        return

    if action == "supplier_bind_link":
        username = get_settings().telegram_bot_username
        if not username:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_bind_link_unavailable"),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("↩️ Переслать сообщение", "supplier_bind_forward", cd.arg)],
                     [menu_button("Отмена", "supplier_detail", cd.arg)]]
                ),
            )
            return
        bind_token = await admin_service.create_supplier_bind_token(
            session, supplier_id=cd.arg, owner_id=owner_id
        )
        link = f"https://t.me/{username.lstrip('@')}?start={bind_token.token}"
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_bind_link", link=link),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("К поставщику", "supplier_detail", cd.arg)],
                    [menu_button("В меню", "main_menu")],
                ]
            ),
        )
        return

    if action == "supplier_bind_skip":
        await admin_service.clear_dialog(session, owner_id)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_unbind":
        await admin_service.unbind_supplier_telegram_id(session, cd.arg)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_set_default":
        await admin_service.set_supplier_default_chat(session, cd.arg, cd.page)
        await _send_supplier_chats(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_toggle_chat":
        await admin_service.toggle_chat_active(session, cd.arg, cd.page)
        await _send_supplier_chats(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "client_groups":
        await _send_client_groups(session, telegram, chat_id, message_id=message_id)
        return

    if action == "client_group_toggle":
        await admin_service.toggle_client_group(session, cd.arg)
        await _send_client_groups(session, telegram, chat_id, message_id=message_id)
        return

    if action == "pending_chats":
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "pending_as_supplier":
        await _send_bind_supplier_list(
            session, telegram, chat_id, cd.arg, page=cd.page, message_id=message_id
        )
        return

    if action == "pending_as_client":
        await admin_service.bind_pending_chat_as_client_group(
            session,
            chat_id=cd.arg,
            title=None,
            bound_by_owner_id=owner_id,
        )
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "pending_ignore":
        await admin_service.delete_pending_chat(session, cd.arg)
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "bind_supplier_to_chat":
        await admin_service.bind_pending_chat_as_supplier(
            session,
            chat_id=cd.arg,
            supplier_id=cd.page,
            bound_by_owner_id=owner_id,
        )
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "price_channels":
        await _send_price_channels(session, telegram, chat_id, message_id=message_id)
        return

    if action == "price_channel_add_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_channel_handle",
            payload={},
        )
        text = render_template("admin_await_channel_handle")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "price_channels")]]),
        )
        return

    if action == "price_channel_delete":
        await parser_client.delete_channel(int(cd.arg))
        await _send_price_channels(session, telegram, chat_id, message_id=message_id)
        return

    if action == "purge_confirm":
        deleted = await purge_service.hard_delete_request(session, cd.arg)
        text = render_template(
            "purge_request_ok" if deleted else "purge_request_not_found",
            request_id=cd.arg,
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
        )
        return

    if action == "purge_cancel":
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("purge_cancelled"),
            message_id=message_id,
        )
        return

    if action == "noop":
        return

    await _send_main_menu(session, telegram, chat_id, message_id=message_id)


async def _pending_count(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count(PendingChat.chat_id)))).scalar_one()
    return int(count)


async def _send_dlq_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    from app.services.dlq_service import list_open_failures

    data = await list_open_failures(session)
    lines = ["Ошибки очереди. Текст поставщика и секреты сюда не попадают."]
    rows: list[list[dict[str, Any]]] = []
    if not data["inbox"] and not data["outbox"]:
        lines.append("Открытых ошибок нет.")
    for item in data["inbox"]:
        lines.append(
            f"вход #{item['id']} попыток {item['attempts']} {item['last_error'] or ''}".rstrip()
        )
        rows.append([menu_button(f"Вход #{item['id']}", "dlqin", int(str(item["id"])))])
    for item in data["outbox"]:
        lines.append(
            f"исход #{item['id']} {item['status']} {item['kind']} "
            f"поставщик {item['supplier_id'] or '—'} {item['last_error'] or ''}".rstrip()
        )
        rows.append([menu_button(f"Исход #{item['id']}", "dlqout", int(str(item["id"])))])
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text="\n".join(lines)[:3500],
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_dlq_item(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    queue: str,
    row_id: int,
    message_id: int | None,
) -> None:
    from app.services.dlq_service import list_open_failures

    data = await list_open_failures(session, limit=50)
    item = next((row for row in data[queue] if row["id"] == row_id), None)
    if item is None:
        await _send_dlq_list(session, telegram, chat_id, message_id=message_id)
        return
    text = (
        f"{'Вход' if queue == 'inbox' else 'Исход'} #{item['id']}\n"
        f"Статус: {item['status']}\n"
        f"Тип: {item['kind']}\n"
        f"Попыток: {item['attempts']}\n"
        f"Заявка: {item['request_id'] or '—'}\n"
        f"Поставщик: {item['supplier_id'] or '—'}\n"
        f"Ошибка: {item['last_error'] or '—'}\n"
        f"Время: {item['created_at']}"
    )
    go = "dlqin_go" if queue == "inbox" else "dlqout_go"
    ok = "dlqin_ok" if queue == "inbox" else "dlqout_ok"
    confirm = (
        "Повтор отправит сообщение ещё раз. Нажмите только если прошлый вызов не дошёл."
        if queue == "outbox"
        else "Повтор снова обработает это обновление."
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=f"{text}\n\n{confirm}",
        message_id=message_id,
        markup=inline_keyboard(
            [
                [menu_button("Повторить", go, row_id)],
                [menu_button("Закрыть без повтора", ok, row_id)],
                [menu_button("К ошибкам", "dlq")],
            ]
        ),
    )


async def _send_main_menu(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    count = await _pending_count(session)
    rows = [
        [menu_button("Заявки", "requests", page=0)],
        [menu_button("Поставщики", "suppliers", page=0)],
        [menu_button("Сотрудники", "employees", page=0)],
        [
            menu_button("Отчёт за день", "report_day"),
            menu_button("Отчёт за неделю", "report_week"),
        ],
        [menu_button("Закупки", "report_purchases")],
        [menu_button("Наценки", "markup_list")],
        [menu_button("Клиентские беседы", "client_groups")],
        [menu_button(f"Новые чаты ({count})", "pending_chats")],
        [menu_button("Каналы прайсов", "price_channels")],
        [menu_button("Business", "business_status")],
        [menu_button("Ошибки системы", "dlq")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_main_menu"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _request_snippet(source_text: str) -> str:
    text = " ".join((source_text or "").split())
    if len(text) <= _MAX_REQUEST_SNIPPET:
        return text or "—"
    return text[: _MAX_REQUEST_SNIPPET - 1] + "…"


def _request_status_label(status: RequestStatus) -> str:
    return _REQUEST_STATUS_LABELS.get(status, status.value)


def _request_is_open(status: RequestStatus) -> bool:
    return status not in (RequestStatus.closed, RequestStatus.cancelled)


async def _send_request_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    requests = await admin_service.list_requests(session)
    if not requests:
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_requests_empty"),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
        )
        return

    items = [
        (
            f"#{row.id} {_request_status_label(row.status)} · {_request_snippet(row.source_text)}",
            _cb("req_detail", arg=row.id, page=page),
        )
        for row in requests
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("requests", page=0),
    )
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_requests_list"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_request_detail(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    request_id: int,
    *,
    list_page: int = 0,
    message_id: int | None = None,
) -> None:
    request = await admin_service.get_request(session, request_id)
    if request is None:
        await _send_request_list(
            session, telegram, chat_id, list_page, message_id=message_id
        )
        return

    rows: list[list[dict[str, Any]]] = []
    if _request_is_open(request.status):
        rows.append(
            [menu_button("Завершить", "req_cancel", request.id, list_page)]
        )
    rows.append(
        [menu_button("Удалить", "req_purge_ask", request.id, list_page)]
    )
    rows.append(
        [
            menu_button("К заявкам", "requests", page=list_page),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template(
            "admin_request_detail",
            request_id=request.id,
            status_label=_request_status_label(request.status),
            source_text=request.source_text or "—",
        ),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _yn(value: bool) -> str:
    return "да" if value else "нет"


async def _send_business_status(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    connections = await admin_service.list_business_connections(session)
    if connections:
        connections_block = "\n".join(
            (
                f"аккаунт {row.tg_user_id}: enabled={_yn(row.is_enabled)}, "
                f"can_reply={_yn(row.can_reply)}, "
                f"can_read={_yn(row.can_read_messages)}"
            )
            for row in connections
        )
    else:
        connections_block = "нет"

    pairs = await admin_service.list_business_dm_suppliers(session)
    if pairs:
        suppliers_block = "\n".join(
            (
                f"{supplier.name} (#{supplier.id}): chat_id={chat.chat_id} "
                f"active={_yn(chat.active)} default={_yn(chat.is_default)}"
            )
            for supplier, chat in pairs
        )
    else:
        suppliers_block = "нет"

    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template(
            "admin_business_status",
            connections_block=connections_block,
            suppliers_block=suppliers_block,
        ),
        message_id=message_id,
        markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
    )


async def _send_supplier_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    suppliers = await admin_service.list_suppliers(session)
    items = [
        (
            f"{s.name} {'✅' if s.active else '⛔'} {'📤' if s.rfq_enabled else ''}",
            _cb("supplier_detail", arg=s.id),
        )
        for s in suppliers
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("suppliers", page=0),
    )
    rows.append(
        [menu_button("➕ Добавить поставщика", "supplier_add_start"), menu_button("В меню", "main_menu")]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_supplier_list"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_employee_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    employees = await admin_service.list_employees(session)
    items = [
        (
            f"{e.name or '—'} {'✅' if e.active else '⛔'} ({e.telegram_id})",
            _cb("employee_detail", arg=e.id),
        )
        for e in employees
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("employees", page=0),
    )
    rows.append(
        [
            menu_button("➕ Добавить сотрудника", "employee_add_start"),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_employees_list"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_employee_detail(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    employee_id: int,
    *,
    message_id: int | None = None,
) -> None:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        await _send_employee_list(session, telegram, chat_id, 0, message_id=message_id)
        return

    text = render_template(
        "admin_employee_detail",
        employee_id=employee.id,
        employee_name=employee.name or "—",
        telegram_id=employee.telegram_id,
        active=employee.active,
    )
    rows = [
        [
            menu_button(
                "⛔ Вкл/выкл" if employee.active else "✅ Вкл/выкл",
                "employee_toggle_active",
                employee_id,
            ),
        ],
        [menu_button("Удалить", "emp_del_ask", employee_id)],
        [menu_button("К списку", "employees", page=0), menu_button("В меню", "main_menu")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _supplier_price_channel_label(supplier: Supplier) -> str:
    if supplier.price_channel_id is None:
        return "не привязан"
    return supplier.price_channel_username or str(supplier.price_channel_id)


def _categories_label(enabled: set[str]) -> str:
    labels = [
        _CATEGORY_LABELS[category]
        for category in _CATEGORY_ORDER
        if category.value in enabled
    ]
    return ", ".join(labels) if labels else "—"


async def _send_supplier_detail(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
        return

    enabled = await admin_service.list_supplier_categories(session, supplier.id)
    text = render_template(
        "admin_supplier_detail",
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        active=supplier.active,
        rfq_enabled=supplier.rfq_enabled,
        telegram_id=supplier.telegram_id or "—",
        dm_ok=supplier.dm_ok,
        price_channel_label=_supplier_price_channel_label(supplier),
        categories_label=_categories_label(enabled),
    )
    rows = [
        [
            menu_button("✏️ Переименовать", "supplier_rename_start", supplier_id),
            menu_button("⛔ Вкл/выкл" if supplier.active else "✅ Вкл/выкл", "supplier_toggle_active", supplier_id),
        ],
        [
            menu_button("📤 RFQ вкл" if supplier.rfq_enabled else "📤 RFQ выкл", "supplier_toggle_rfq", supplier_id),
        ],
        [
            menu_button("📂 Категории", "sup_cat", supplier_id),
        ],
        [
            menu_button("📡 Канал прайса", "supplier_channel", supplier_id),
        ],
        [
            menu_button(
                "📱 Привязать личку" if supplier.telegram_id is None else "🔄 Сменить личку",
                "supplier_bind_start",
                supplier_id,
            ),
            menu_button("🗑 Отвязать личку", "supplier_unbind", supplier_id)
            if supplier.telegram_id is not None
            else button("", cd=_cb("noop")),
        ],
        [
            menu_button("Чаты поставщика", "supplier_chats", supplier_id),
        ],
        [menu_button("Удалить", "sup_del_ask", supplier_id)],
        [menu_button("К списку", "suppliers", page=0), menu_button("В меню", "main_menu")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_supplier_categories(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
        return
    enabled = await admin_service.list_supplier_categories(session, supplier_id)
    rows: list[list[dict[str, Any]]] = []
    for index, category in enumerate(_CATEGORY_ORDER):
        mark = "✅" if category.value in enabled else "❌"
        rows.append(
            [
                menu_button(
                    f"{mark} {_CATEGORY_LABELS[category]}",
                    "sup_cat_tog",
                    supplier_id,
                    page=index,
                )
            ]
        )
    rows.append([menu_button("К поставщику", "supplier_detail", supplier_id)])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template(
            "admin_supplier_categories",
            supplier_id=supplier.id,
            supplier_name=supplier.name,
        ),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_supplier_channel_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    try:
        channels = await parser_client.list_channels()
    except parser_client.ParserClientError as exc:
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_price_channels_error", detail=str(exc)),
            message_id=message_id,
            markup=inline_keyboard(
                [[menu_button("К поставщику", "supplier_detail", supplier_id)]]
            ),
        )
        return

    price_channels = [
        ch for ch in channels if ch.purpose == "supplier_price_source"
    ]
    rows: list[list[dict[str, Any]]] = [
        [menu_button("Не привязан / отвязать", "supplier_unbind_ch", supplier_id)]
    ]
    for ch in price_channels:
        handle = ch.username or ch.title or f"#{ch.id}"
        if ch.channel_id is not None and ch.is_active:
            label = f"✅ {handle}"
        else:
            label = f"⏳ {handle} (готовится)"
        rows.append(
            [menu_button(label, "supplier_bind_ch", supplier_id, ch.id)]
        )
    rows.append(
        [
            menu_button("К поставщику", "supplier_detail", supplier_id),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_supplier_channel_pick", supplier_id=supplier_id),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_supplier_chats(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    chats = await admin_service.list_supplier_chats(session, supplier_id)
    text = render_template(
        "admin_supplier_chats",
        supplier_id=supplier_id,
        count=len(chats),
    )
    rows: list[list[dict[str, Any]]] = []
    for c in chats:
        star = "⭐" if c.is_default else "  "
        active = "✅" if c.active else "⛔"
        label = f"{star} {active} {c.title or c.chat_id} ({c.chat_type.value})"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=c.chat_id)),
                menu_button("⭐" if not c.is_default else "—", "supplier_set_default", supplier_id, c.chat_id),
                menu_button("Вкл" if not c.active else "Выкл", "supplier_toggle_chat", supplier_id, c.chat_id),
            ]
        )
    rows.append(
        [menu_button("К поставщику", "supplier_detail", supplier_id), menu_button("В меню", "main_menu")]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_client_groups(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    groups = await admin_service.list_client_groups(session)
    rows: list[list[dict[str, Any]]] = []
    for g in groups:
        label = f"{'✅' if g.active else '⛔'} {g.title or g.chat_id}"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=g.id)),
                menu_button("Вкл" if not g.active else "Выкл", "client_group_toggle", g.id),
            ]
        )
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_client_groups"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_pending_chats(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    result = await session.execute(
        select(PendingChat).order_by(PendingChat.created_at.desc())
    )
    pending = list(result.scalars().all())
    rows: list[list[dict[str, Any]]] = []
    for p in pending:
        label = f"{p.title or p.chat_id} ({p.chat_type.value})"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=p.chat_id)),
                menu_button("Поставщик", "pending_as_supplier", p.chat_id),
                menu_button("Клиенты", "pending_as_client", p.chat_id),
                menu_button("Игнор", "pending_ignore", p.chat_id),
            ]
        )
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_pending_chats"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_bind_supplier_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    pending_chat_id: int,
    *,
    page: int,
    message_id: int | None = None,
) -> None:
    suppliers = await admin_service.list_suppliers(session)
    items = [
        (s.name, _cb("bind_supplier_to_chat", arg=pending_chat_id, page=s.id))
        for s in suppliers
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("pending_as_supplier", arg=pending_chat_id, page=0),
    )
    rows.append([menu_button("Назад", "pending_chats")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_bind_supplier"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_price_channels(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    try:
        channels = await parser_client.list_channels()
    except parser_client.ParserClientError as exc:
        text = render_template("admin_price_channels_error", detail=str(exc))
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
        )
        return

    rows: list[list[dict[str, Any]]] = []
    for ch in channels:
        label = f"{'✅' if ch.is_active else '⏳'} {ch.title or ch.username or ch.channel_id}"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=ch.id)),
                menu_button("Удалить", "price_channel_delete", ch.id),
            ]
        )
    rows.append(
        [menu_button("➕ Добавить канал", "price_channel_add_start"), menu_button("В меню", "main_menu")]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_price_channels"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _handle_dialog_text(
    session: AsyncSession,
    dialog: Any,
    owner_id: int,
    chat_id: int,
    message: dict[str, Any],
    telegram: TelegramClientProtocol,
) -> str:
    state = dialog.state
    payload = dialog.payload or {}
    text = extract_message_text(message)

    if state == "await_markup_amount":
        from decimal import Decimal, InvalidOperation

        from app.handlers.owner_commands import apply_markup_choice

        raw = text.replace(" ", "").replace(",", ".")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            await telegram.send_message(chat_id, "Сумма должна быть числом")
            return "ok"
        if amount < 0:
            await telegram.send_message(chat_id, "Сумма не может быть отрицательной")
            return "ok"
        rule_id = int(payload.get("rule_id") or 0)
        await admin_service.clear_dialog(session, owner_id)
        await apply_markup_choice(
            session,
            chat_id=chat_id,
            rule_id=rule_id,
            amount=amount,
            telegram=telegram,
        )
        return "ok"

    if state == "await_markup_rule_phrase":
        phrase = text.strip()
        if len(phrase) < 2:
            await telegram.send_message(
                chat_id, render_template("markup_add_enter_phrase")
            )
            return "ok"
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_markup_rule_amount",
            payload={"phrase": phrase[:200]},
        )
        await telegram.send_message(
            chat_id,
            render_template("markup_add_enter_amount", rule_label=phrase[:80]),
        )
        return "ok"

    if state == "await_markup_rule_amount":
        from decimal import Decimal, InvalidOperation

        from app.services.markup_service import create_markup_rule

        phrase = str(payload.get("phrase") or "").strip()
        if not phrase:
            await admin_service.clear_dialog(session, owner_id)
            await telegram.send_message(chat_id, render_template("admin_error", detail="нет названия товара"))
            return "ok"
        raw = text.replace(" ", "").replace(",", ".")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            await telegram.send_message(chat_id, "Сумма должна быть числом")
            return "ok"
        if amount < 0:
            await telegram.send_message(chat_id, "Сумма не может быть отрицательной")
            return "ok"
        try:
            rule = await create_markup_rule(
                session, phrase=phrase, markup_rub=amount
            )
        except ValueError:
            await telegram.send_message(
                chat_id, render_template("markup_add_enter_phrase")
            )
            return "ok"
        await admin_service.clear_dialog(session, owner_id)
        label = (rule.rule_key or phrase).replace("_", " ")
        await telegram.send_message(
            chat_id,
            render_template(
                "markup_rule_created",
                rule_label=label,
                amount=int(amount),
            ),
            reply_markup=inline_keyboard(
                [
                    [
                        menu_button("К наценкам", "markup_list"),
                        menu_button("В меню", "main_menu"),
                    ]
                ]
            ),
        )
        return "ok"

    if state == "await_supplier_name":
        name = text.strip()
        if not name:
            await telegram.send_message(chat_id, render_template("admin_need_name"))
            return "ok"
        supplier = await admin_service.add_supplier(
            session, name=name, bound_by_owner_id=owner_id
        )
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_bind",
            payload={"supplier_id": supplier.id},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_bind_prompt"),
            message_id=message.get("message_id"),
            markup=inline_keyboard(
                [
                    [
                        menu_button("↩️ Переслать сообщение", "supplier_bind_forward", supplier.id),
                        menu_button("🔗 Ссылка", "supplier_bind_link", supplier.id),
                    ],
                    [menu_button("⏭ Пропустить", "supplier_bind_skip", supplier.id)],
                    [menu_button("Отмена", "main_menu")],
                ]
            ),
        )
        return "ok"

    if state == "await_channel_handle":
        handle = text.strip()
        if not handle:
            await telegram.send_message(chat_id, render_template("admin_need_channel"))
            return "ok"
        try:
            await parser_client.add_channel(handle)
        except parser_client.ParserClientError as exc:
            await telegram.send_message(
                chat_id,
                render_template("admin_channel_add_error", detail=str(exc)),
                reply_markup=inline_keyboard([[menu_button("К каналам", "price_channels")]]),
            )
            return "ok"
        finally:
            await admin_service.clear_dialog(session, owner_id)
        await _send_price_channels(session, telegram, chat_id)
        return "ok"

    if state == "await_rename":
        supplier_id = payload.get("supplier_id")
        name = text.strip()
        if not name or supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"
        await admin_service.rename_supplier(session, supplier_id, name)
        await admin_service.clear_dialog(session, owner_id)
        await _send_supplier_detail(session, telegram, chat_id, supplier_id)
        return "ok"

    if state == "await_employee_name":
        name = text.strip()
        if not name:
            await telegram.send_message(
                chat_id, render_template("admin_employee_need_name")
            )
            return "ok"
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_employee_telegram_id",
            payload={"name": name},
        )
        await telegram.send_message(
            chat_id,
            render_template("admin_await_employee_telegram_id"),
            reply_markup=inline_keyboard(
                [[menu_button("Отмена", "employees", page=0)]]
            ),
        )
        return "ok"

    if state == "await_employee_telegram_id":
        name = str(payload.get("name") or "")
        if not name:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"

        telegram_id = resolve_user_id_from_message(text, message)

        if telegram_id is None:
            if message.get("forward_from") or message.get("forward_origin"):
                await telegram.send_message(
                    chat_id, render_template("admin_employee_forward_hidden")
                )
            else:
                await telegram.send_message(
                    chat_id, render_template("admin_employee_invalid_telegram_id")
                )
            return "ok"

        try:
            employee = await admin_service.add_employee(
                session, name=name, telegram_id=telegram_id
            )
        except admin_service.DuplicateEmployeeTelegramIdError:
            await telegram.send_message(
                chat_id,
                render_template(
                    "admin_employee_duplicate_telegram_id",
                    telegram_id=telegram_id,
                ),
            )
            return "ok"

        await admin_service.clear_dialog(session, owner_id)
        await _send_employee_detail(session, telegram, chat_id, employee.id)
        return "ok"

    if state == "await_supplier_bind":
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"
        # This state is normally driven by callbacks; plain text here is ignored
        # but the dialog stays alive so owner can click the inline buttons.
        return "ok"

    if state == "await_supplier_forward":
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"

        telegram_id = resolve_user_id_from_message(text, message)

        if telegram_id is None:
            if message.get("forward_from") or message.get("forward_origin"):
                await telegram.send_message(
                    chat_id,
                    render_template(
                        "admin_supplier_forward_hidden",
                        supplier_id=supplier_id,
                    ),
                    reply_markup=inline_keyboard(
                        [
                            [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                            [menu_button("Отмена", "supplier_detail", supplier_id)],
                        ]
                    ),
                )
            else:
                await telegram.send_message(
                    chat_id,
                    render_template("admin_supplier_invalid_telegram_id"),
                    reply_markup=inline_keyboard(
                        [[menu_button("Отмена", "supplier_detail", supplier_id)]]
                    ),
                )
            return "ok"

        try:
            supplier = await admin_service.bind_supplier_telegram_id(
                session,
                supplier_id=int(supplier_id),
                telegram_id=telegram_id,
                bound_by_owner_id=owner_id,
            )
        except admin_service.TelegramIdConflictError:
            await telegram.send_message(
                chat_id,
                render_template(
                    "admin_supplier_telegram_id_busy",
                    telegram_id=telegram_id,
                ),
                reply_markup=inline_keyboard(
                    [
                        [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                        [menu_button("Отмена", "supplier_detail", supplier_id)],
                    ]
                ),
            )
            return "ok"

        await admin_service.clear_dialog(session, owner_id)
        await telegram.send_message(
            chat_id,
            render_template(
                "admin_supplier_bound",
                supplier_id=supplier.id,
                supplier_name=supplier.name,
                telegram_id=telegram_id,
            ),
        )
        await _send_supplier_detail(session, telegram, chat_id, supplier.id)
        return "ok"

    # Legacy dialog state: redirect to the new self-service bind flow.
    if state == "await_supplier_telegram_id":
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"
        telegram_id = resolve_user_id_from_message(text, message)
        if telegram_id is not None:
            try:
                await admin_service.bind_supplier_telegram_id(
                    session,
                    supplier_id=int(supplier_id),
                    telegram_id=telegram_id,
                    bound_by_owner_id=owner_id,
                )
            except admin_service.TelegramIdConflictError:
                await telegram.send_message(
                    chat_id,
                    render_template(
                        "admin_supplier_telegram_id_busy",
                        telegram_id=telegram_id,
                    ),
                    reply_markup=inline_keyboard(
                        [
                            [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                            [menu_button("Отмена", "supplier_detail", supplier_id)],
                        ]
                    ),
                )
                return "ok"
            await admin_service.clear_dialog(session, owner_id)
            await _send_supplier_detail(session, telegram, chat_id, int(supplier_id))
            return "ok"

        if message.get("forward_from") or message.get("forward_origin"):
            await telegram.send_message(
                chat_id,
                render_template(
                    "admin_supplier_forward_hidden",
                    supplier_id=supplier_id,
                ),
                reply_markup=inline_keyboard(
                    [
                        [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                        [menu_button("Отмена", "supplier_detail", supplier_id)],
                    ]
                ),
            )
            return "ok"

        await telegram.send_message(
            chat_id,
            render_template("admin_supplier_invalid_telegram_id"),
            reply_markup=inline_keyboard(
                [[menu_button("Отмена", "supplier_detail", supplier_id)]]
            ),
        )
        return "ok"

    await admin_service.clear_dialog(session, owner_id)
    await telegram.send_message(chat_id, render_template("admin_dialog_expired"))
    return "ok"
